from pathlib import Path
from uuid import uuid4
import difflib
import json
import re
import subprocess
import sys
import time
import httpx

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .agent_templates import TEMPLATES, get_template
from .api_keys import display_prefix, generate_key, hash_key
from .auth import current_user, current_workspace_id
from .database import get_db
from .deploy_policy import DEFAULT_DEPLOY_CONFIG, build_publish_checklist, parse_deploy_config
from .eval_metrics import build_evaluation_summary, classify_failure, suggest_fix
from .knowledge import search_chunks
from .model_gateway import complete, demo_answer, embed_query, resolve_provider
from .models import Agent, AgentApiKey, AgentEvalRun, AgentVersion, User, WorkflowRun
from .schemas import AgentOut, AgentTemplateOut, AgentVersionDetail, AgentVersionDiff, AgentVersionOut, ApiKeyCreateOut, ApiKeyOut, ApiKeyUpdate, CallLogEntryOut, CreateFromTemplateRequest, DeployConfigOut, DeployConfigUpdate, PublishChecklistOut, SaveVersionRequest, WorkspaceMemberOut


router = APIRouter(prefix="/api/apps", tags=["apps"])

BASE_DIR = Path(__file__).resolve().parent
DRAFT_ROOT = BASE_DIR / "storage" / "app_drafts"


class CreateDraftRequest(BaseModel):
    name: str


class SaveFileRequest(BaseModel):
    path: str
    content: str


class CreateFileRequest(BaseModel):
    path: str
    content: str = ""


class RenameFileRequest(BaseModel):
    old_path: str
    new_path: str


class RunDraftRequest(BaseModel):
    input_text: str = "test input"


class TestCase(BaseModel):
    name: str
    input: str
    expected: str = ""


class EvaluateDraftRequest(BaseModel):
    cases: list[TestCase] = []


class GenerateAgentRequest(BaseModel):
    message: str
    project_name: str = ""



DEFAULT_MAIN = '''def main(input_text: str):
    return "Hello Atlas: " + input_text


if __name__ == "__main__":
    print(main("test input"))
'''


def default_manifest(app_name: str) -> str:
    return json.dumps(
        {
            "name": app_name,
            "description": "企业内部智能应用草稿",
            "entry": "main.py",
            "runtime": "python",
            "model": "qwen-turbo",
            "prompt": "你是一个可靠的企业智能体，请根据输入给出清晰、可执行的回答。",
            "knowledge_bases": [],
            "skills": [],
            "connectors": [],
            "permissions": {
                "network": False,
                "secrets": [],
                "filesystem": "sandbox",
            },
        },
        ensure_ascii=False,
        indent=2,
    )


def safe_path(root: Path, relative_path: str) -> Path:
    normalized = relative_path.strip().replace("\\", "/")

    if not normalized or normalized.startswith("/") or ".." in Path(normalized).parts:
        raise HTTPException(status_code=400, detail="非法文件路径")

    target = (root / normalized).resolve()
    root_resolved = root.resolve()

    if root_resolved != target and root_resolved not in target.parents:
        raise HTTPException(status_code=400, detail="非法文件路径")

    return target


def draft_dir_or_404(draft_id: str, ws: str, db: Session) -> Path:
    agent_or_404(draft_id, ws, db)  # 草稿目录名 = agent.id：先校验归属，再摸文件系统
    draft_dir = DRAFT_ROOT / draft_id

    if not draft_dir.exists():
        raise HTTPException(status_code=404, detail="草稿不存在")

    return draft_dir


def agent_or_404(agent_id: str, ws: str, db: Session) -> Agent:
    agent = db.scalar(select(Agent).where(Agent.id == agent_id, Agent.workspace_id == ws))
    if not agent:
        raise HTTPException(status_code=404, detail="智能体不存在")
    return agent


def read_workspace_files(draft_dir: Path) -> dict[str, str]:
    """把工作区目录打成 {path: content} 快照（跳过内部运行时文件）。"""
    files: dict[str, str] = {}
    for item in sorted(draft_dir.rglob("*")):
        if item.is_dir() or item.name.startswith(".atlas_"):
            continue
        try:
            files[item.relative_to(draft_dir).as_posix()] = item.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # 二进制文件不入快照
    return files


def write_workspace_files(draft_dir: Path, files: dict[str, str]) -> None:
    """用快照内容覆盖工作区：先清空已跟踪的文本文件，再写入快照里的全部文件。"""
    for item in list(draft_dir.rglob("*")):
        if item.is_file() and not item.name.startswith(".atlas_"):
            item.unlink()
    for path, content in files.items():
        target = safe_path(draft_dir, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def snapshot_code_agent(draft_dir: Path) -> dict:
    manifest = read_manifest(draft_dir) if (draft_dir / "manifest.json").exists() else {}
    return {"manifest": manifest, "files": read_workspace_files(draft_dir)}


def snapshot_prompt_agent(agent: Agent) -> dict:
    return {
        "system_prompt": agent.system_prompt,
        "model": agent.model,
        "knowledge_base_id": agent.knowledge_base_id,
    }


def next_version_no(db: Session, agent_id: str) -> int:
    last = db.scalar(
        select(AgentVersion.version_no).where(AgentVersion.agent_id == agent_id)
        .order_by(AgentVersion.version_no.desc())
    )
    return (last or 0) + 1


def create_version(db: Session, agent: Agent, snapshot: dict, label: str = "draft", note: str = "") -> AgentVersion:
    version = AgentVersion(
        agent_id=agent.id,
        version_no=next_version_no(db, agent.id),
        kind=agent.kind,
        label=label,
        note=note,
        snapshot_json=json.dumps(snapshot, ensure_ascii=False),
    )
    db.add(version)
    db.flush()
    agent.current_version_id = version.id
    return version


def agent_out_fields(agent: Agent, db: Session) -> dict:
    """给 AgentOut 补充版本号、发布版本号和资源计数——用于「我的 Agents」卡片展示。"""
    version_no = 0
    if agent.current_version_id:
        version_no = db.scalar(select(AgentVersion.version_no).where(AgentVersion.id == agent.current_version_id)) or 0
    published_version_no = None
    if agent.published_version_id:
        published_version_no = db.scalar(select(AgentVersion.version_no).where(AgentVersion.id == agent.published_version_id))

    kb_count, skills_count, connector_count = 0, 0, 0
    if agent.kind == "code":
        draft_dir = DRAFT_ROOT / agent.id
        manifest_file = draft_dir / "manifest.json"
        if manifest_file.exists():
            try:
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                kb_count = len(manifest.get("knowledge_bases") or [])
                skills_count = len(manifest.get("skills") or [])
                connector_count = len(manifest.get("connectors") or [])
            except json.JSONDecodeError:
                pass
    else:
        kb_count = 1 if agent.knowledge_base_id else 0

    deploy_config = parse_deploy_config(agent.deploy_config_json)
    call_count = 0
    if deploy_config.get("call_log_enabled", True):
        call_count = db.scalar(select(func.count()).select_from(WorkflowRun).where(WorkflowRun.agent_id == agent.id)) or 0

    latest_test = latest_eval_run(db, agent.id, "test")
    latest_eval = latest_eval_run(db, agent.id, "evaluate")

    return {
        "kind": agent.kind,
        "version_no": version_no,
        "published_version_no": published_version_no,
        "knowledge_base_count": kb_count,
        "skills_count": skills_count,
        "connector_count": connector_count,
        "call_count": call_count,
        "created_by": agent.created_by,
        "has_passed_test": bool(latest_test and latest_test.ok) or agent.kind == "prompt",
        "last_eval_ok": latest_eval.ok if latest_eval else None,
        "deploy_config_configured": agent.deploy_config_json != "{}",
    }


def record_eval_run(db: Session, agent_id: str, kind: str, ok: bool, passed: int, total: int, results: list) -> None:
    """把测试(run_draft_app)/评测(evaluate_draft_app)结果落库，供发布前检查清单读取。"""
    db.add(AgentEvalRun(
        agent_id=agent_id, kind=kind, ok=ok, passed=passed, total=total,
        pass_rate=round(passed / total, 4) if total else 0.0,
        results_json=json.dumps(results, ensure_ascii=False),
    ))
    db.commit()


def latest_eval_run(db: Session, agent_id: str, kind: str) -> AgentEvalRun | None:
    return db.scalar(
        select(AgentEvalRun).where(AgentEvalRun.agent_id == agent_id, AgentEvalRun.kind == kind)
        .order_by(AgentEvalRun.created_at.desc())
    )


def ensure_demo_files(draft_dir: Path, app_name: str) -> None:
    draft_dir.mkdir(parents=True, exist_ok=True)

    main_file = draft_dir / "main.py"
    manifest_file = draft_dir / "manifest.json"
    tests_file = draft_dir / "tests.json"

    if not main_file.exists():
        main_file.write_text(DEFAULT_MAIN, encoding="utf-8")

    if not manifest_file.exists():
        manifest_file.write_text(default_manifest(app_name), encoding="utf-8")

    if not tests_file.exists():
        tests_file.write_text(
            json.dumps(
                [
                    {
                        "name": "基础问候",
                        "input": "Atlas",
                        "expected": "Hello Atlas: Atlas",
                    }
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def read_manifest(draft_dir: Path) -> dict:
    manifest_file = draft_dir / "manifest.json"

    if not manifest_file.exists():
        raise HTTPException(status_code=400, detail="缺少 manifest.json")

    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"manifest JSON 格式错误：第 {exc.lineno} 行") from exc

    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="manifest 必须是 JSON 对象")

    return data


def validate_manifest(draft_dir: Path) -> tuple[dict, list[str], list[str]]:
    data = read_manifest(draft_dir)
    errors: list[str] = []
    warnings: list[str] = []

    entry = data.get("entry", "main.py")

    if not data.get("name"):
        errors.append("缺少应用名称 name")

    if data.get("runtime") != "python":
        errors.append("当前沙箱仅支持 python runtime")

    if not isinstance(entry, str) or not entry.endswith(".py"):
        errors.append("entry 必须指向 Python 文件")
    elif not safe_path(draft_dir, entry).exists():
        errors.append(f"入口文件不存在：{entry}")

    permissions = data.get("permissions") or {}

    if permissions.get("network"):
        warnings.append("当前预览沙箱会阻止外网访问，发布前需要网络白名单审批")

    if permissions.get("secrets"):
        warnings.append("检测到 secrets 声明，发布前需要确认注入范围")

    return data, errors, warnings


async def run_model_entry(draft_dir: Path, input_text: str) -> dict | None:
    manifest, errors, warnings = validate_manifest(draft_dir)
    if errors:
        return None

    model = str(manifest.get("model") or "")
    prompt = str(manifest.get("prompt") or "")
    name = str(manifest.get("name") or "Atlas Agent")
    description = str(manifest.get("description") or "")
    skills = manifest.get("skills") if isinstance(manifest.get("skills"), list) else []

    base_url, api_key, real_model = resolve_provider(model)
    if not api_key:
        return None

    system_prompt = "\n".join([
        prompt,
        "",
        zh("\u4f60\u73b0\u5728\u662f\u5df2\u7ecf\u521b\u5efa\u597d\u7684\u53ef\u7528 Agent\uff1a") + name + zh("\u3002"),
        f"应用描述：{description}",
        zh("\u5df2\u542f\u7528\u80fd\u529b\uff1a") + (", ".join(str(item) for item in skills) or zh("\u57fa\u7840\u4efb\u52a1\u5904\u7406")),
        '请直接回答用户问题，不要复述你是草稿，也不要只给通用执行路径。',
        '回答要结构化、可操作；如果问题需要实时数据或外部系统，而当前没有工具结果，请明确说明限制，并给出下一步需要用户补充的信息。',
        '涉及投资、医疗、法律等高风险主题时，必须提示风险，不要承诺确定收益或替用户做最终决策。',
    ])
    payload = {
        "model": real_model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": input_text},
        ],
        "temperature": 0.4,
        "max_tokens": 1200,
    }
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
            response = await client.post(
                base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
        data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if not content:
            return None
        return {
            "ok": True,
            "logs": "\n".join([
                f"> model: {real_model}",
                f"> provider: {base_url}",
                f"> input: {input_text}",
                f"> elapsed: {elapsed_ms} ms",
                "",
                content,
                "",
                "> exit code: 0",
            ]),
            "content": content,
            "error": None,
            "warnings": warnings,
            "elapsed_ms": elapsed_ms,
            "usage": data.get("usage"),  # 供应商如实返回才带出；评测走确定性沙箱没有这个数据，不在评测聚合里假造
        }
    except Exception as error:
        return {
            "ok": False,
            "logs": "\n".join([
                "> 大模型调用失败，已保留本地沙箱回退能力",
                f"> ERROR: {error}",
            ]),
            "error": str(error),
            "warnings": [*warnings, "大模型调用失败，请检查 API Key、Base URL、模型名或网络"],
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }


def run_python_entry(draft_dir: Path, input_text: str, timeout: int = 10) -> dict:
    manifest, errors, warnings = validate_manifest(draft_dir)

    if errors:
        return {
            "ok": False,
            "logs": "\n".join(["> Manifest 校验失败", *[f"> ERROR: {item}" for item in errors]]),
            "error": "; ".join(errors),
            "warnings": warnings,
        }

    entry = str(manifest.get("entry", "main.py"))
    entry_file = safe_path(draft_dir, entry)
    runner_file = draft_dir / ".atlas_runner.py"
    started = time.perf_counter()

    runner_file.write_text(
        "import importlib.util\n"
        "import json\n"
        "from pathlib import Path\n"
        f"entry = Path({entry!r})\n"
        "spec = importlib.util.spec_from_file_location('atlas_app_entry', entry)\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "assert spec and spec.loader\n"
        "spec.loader.exec_module(module)\n"
        "if not hasattr(module, 'main'):\n"
        "    raise RuntimeError('入口文件必须暴露 main(input_text) 函数')\n"
        f"result = module.main({input_text!r})\n"
        "if isinstance(result, (dict, list)):\n"
        "    print(json.dumps(result, ensure_ascii=False, indent=2))\n"
        "else:\n"
        "    print(result)\n",
        encoding="utf-8",
    )

    try:
        result = subprocess.run(
            [sys.executable, str(runner_file)],
            cwd=str(draft_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "logs": "> 沙箱运行超时：超过 10 秒",
            "error": "timeout",
            "warnings": warnings,
        }
    finally:
        runner_file.unlink(missing_ok=True)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logs = [
        f"> draft: {draft_dir.name}",
        f"> runtime: python",
        f"> entry: {entry_file.name}",
        f"> input: {input_text}",
        f"> elapsed: {elapsed_ms} ms",
        "",
    ]

    if warnings:
        logs.extend([f"> WARN: {item}" for item in warnings])
        logs.append("")

    if result.stdout:
        logs.append(result.stdout.rstrip())

    if result.stderr:
        logs.append("\n[stderr]")
        logs.append(result.stderr.rstrip())

    logs.append(f"\n> exit code: {result.returncode}")

    return {
        "ok": result.returncode == 0,
        "logs": "\n".join(logs),
        "content": result.stdout.rstrip() if result.returncode == 0 else "",
        "error": result.stderr if result.returncode != 0 else None,
        "warnings": warnings,
        "elapsed_ms": elapsed_ms,
    }


async def invoke_agent(agent: Agent, input_text: str, db: Session) -> dict:
    """外部 API Key 调用入口（PRD §6）的执行调度：统一返回
    {ok, output, error, elapsed_ms}，屏蔽 kind=code/prompt 的差异和沙箱日志噪声。"""
    started = time.perf_counter()

    if agent.kind == "code":
        draft_dir = DRAFT_ROOT / agent.id
        model_result = await run_model_entry(draft_dir, input_text)
        if model_result and model_result.get("ok"):
            return {"ok": True, "output": model_result.get("content", ""), "error": None,
                    "elapsed_ms": model_result.get("elapsed_ms", 0)}
        fallback = run_python_entry(draft_dir, input_text)
        return {"ok": fallback.get("ok", False), "output": fallback.get("content", ""),
                "error": fallback.get("error"), "elapsed_ms": fallback.get("elapsed_ms", 0)}

    messages = [{"role": "system", "content": agent.system_prompt}]
    if agent.knowledge_base_id:
        query_vector = await embed_query(input_text)
        hits = search_chunks(db, agent.knowledge_base_id, input_text, query_vector)
        if hits:
            context = "\n\n".join(f"[{item['document']} 第{item['page']}页]\n{item['content']}" for item in hits)
            messages.append({"role": "system", "content": "知识库资料（仅依据这些资料回答）：\n" + context})
    messages.append({"role": "user", "content": input_text})

    try:
        content = await complete(messages, agent.model)
        if not content:
            content = demo_answer(messages)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return {"ok": True, "output": content, "error": None, "elapsed_ms": elapsed_ms}
    except Exception as error:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return {"ok": False, "output": "", "error": str(error), "elapsed_ms": elapsed_ms}




def zh(value: str) -> str:
    try:
        return value.encode("ascii").decode("unicode_escape")
    except UnicodeEncodeError:
        return value


def clean_agent_name(value: str) -> str:
    value = value.strip()
    for mark in [" ", "\t", "\n", ",", ".", "!", "?", ";", ":", zh("\uff0c"), zh("\u3002"), zh("\uff01"), zh("\uff1f"), zh("\uff1b"), zh("\uff1a"), zh("\u3001")]:
        value = value.strip(mark)
    for prefix in [zh("\u4e00\u4e2a"), zh("\u4e00\u6b3e"), zh("\u53ef\u4ee5"), zh("\u80fd\u591f"), zh("\u80fd")]:
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
    value = value.replace("agent", "Agent").replace("AGENT", "Agent")
    if not value:
        return zh("\u81ea\u5b9a\u4e49 Agent")
    suffixes = ["Agent", zh("\u52a9\u624b"), zh("\u5de5\u5177"), zh("\u673a\u5668\u4eba"), zh("\u5e94\u7528"), zh("\u4e13\u5bb6"), zh("\u5206\u6790\u5e08"), zh("\u751f\u6210\u5668")]
    if not any(value.endswith(suffix) for suffix in suffixes):
        value += zh("\u52a9\u624b")
    return value[:28]


def extract_agent_name(message: str) -> str:
    starters = [
        zh("\u5e2e\u6211\u5236\u4f5c\u4e00\u4e2a"),
        zh("\u5e2e\u6211\u521b\u5efa\u4e00\u4e2a"),
        zh("\u5e2e\u6211\u505a\u4e00\u4e2a"),
        zh("\u5236\u4f5c\u4e00\u4e2a"),
        zh("\u521b\u5efa\u4e00\u4e2a"),
        zh("\u751f\u6210\u4e00\u4e2a"),
        zh("\u642d\u5efa\u4e00\u4e2a"),
        zh("\u505a\u4e00\u4e2a"),
        zh("\u5236\u4f5c"),
        zh("\u521b\u5efa"),
        zh("\u751f\u6210"),
        zh("\u642d\u5efa"),
        zh("\u505a"),
    ]
    stops = [zh("\u80fd"), zh("\u53ef\u4ee5"), zh("\u7528\u4e8e"), zh("\u5e2e"), zh("\u4e13\u95e8"), zh("\u4e3b\u8981"), zh("\u652f\u6301"), zh("\uff0c"), zh("\u3002"), ",", ".", ";"]
    lowered = message.lower()
    for starter in starters:
        index = lowered.find(starter.lower())
        if index < 0:
            continue
        candidate = message[index + len(starter):]
        stop_positions = [candidate.find(stop) for stop in stops if candidate.find(stop) > 0]
        if stop_positions:
            candidate = candidate[:min(stop_positions)]
        return clean_agent_name(candidate)
    suffixes = ["Agent", "agent", zh("\u52a9\u624b"), zh("\u5de5\u5177"), zh("\u673a\u5668\u4eba"), zh("\u5e94\u7528"), zh("\u4e13\u5bb6"), zh("\u5206\u6790\u5e08"), zh("\u751f\u6210\u5668")]
    for suffix in suffixes:
        pos = message.find(suffix)
        if pos > 0:
            start_pos = max(0, pos - 12)
            candidate = message[start_pos:pos + len(suffix)]
            return clean_agent_name(candidate)
    return zh("\u81ea\u5b9a\u4e49 Agent")

def slug_skill(value: str) -> str:
    mapping = {
        zh("\u77e5\u8bc6\u5e93"): "knowledge-base-qa",
        zh("\u95ee\u7b54"): "question-answering",
        zh("\u6587\u6863"): "document-search",
        zh("\u5ba2\u670d"): "customer-service",
        zh("\u552e\u540e"): "after-sales-support",
        zh("\u5de5\u5355"): "ticket-summary",
        zh("\u9500\u552e"): "sales-assistant",
        zh("\u7ebf\u7d22"): "lead-qualification",
        zh("\u5ba2\u6237"): "customer-summary",
        zh("\u6d41\u7a0b"): "workflow-guidance",
        zh("\u5ba1\u6279"): "approval-checklist",
        zh("\u5236\u5ea6"): "policy-search",
        zh("\u62a5\u9500"): "reimbursement-guidance",
        zh("\u5165\u804c"): "onboarding-guidance",
        zh("\u7b80\u5386"): "resume-screening",
        zh("\u62db\u8058"): "recruiting-assistant",
        zh("\u9762\u8bd5"): "interview-questioning",
        zh("PPT"): "ppt-generation",
        zh("\u6f14\u793a"): "presentation-outline",
        zh("\u5199\u4f5c"): "copywriting",
        zh("\u6587\u6848"): "content-writing",
        zh("Excel"): "spreadsheet-analysis",
        zh("\u8868\u683c"): "spreadsheet-processing",
        zh("\u7f51\u9875"): "web-page-generation",
        zh("\u56fe\u8868"): "chart-generation",
        zh("\u6570\u636e"): "data-analysis",
        zh("\u8bdd\u9898"): "topic-tracking",
        zh("\u64ad\u5ba2"): "podcast-script",
        zh("\u8bbe\u8ba1"): "design-generation",
        zh("\u80a1"): "stock-analysis",
        zh("\u884c\u60c5"): "market-monitoring",
        zh("\u8d22\u62a5"): "financial-report-analysis",
        zh("\u516c\u544a"): "announcement-tracking",
        zh("\u98ce\u9669"): "risk-check",
    }
    return mapping.get(value, "task-planning")


def infer_agent_blueprint(message: str, project_name: str = "") -> dict:
    text = message.strip() or zh("\u505a\u4e00\u4e2a\u4f01\u4e1a\u4efb\u52a1 Agent")
    lowered = text.lower()
    old_default = {zh("Atlas \u4f01\u4e1a\u52a9\u624b\u9879\u76ee"), zh("\u6263\u5b50\u7684\u65b0\u9879\u76ee"), "demo-agent-app"}
    name = project_name.strip() if project_name.strip() and project_name.strip() not in old_default else extract_agent_name(text)

    keyword_groups = [
        (zh("\u5ba2\u670d"), zh("\u5ba2\u6237\u670d\u52a1\u4e0e\u552e\u540e\u5de5\u5355"), [zh("\u5ba2\u670d"), zh("\u552e\u540e"), zh("\u5de5\u5355"), zh("\u77e5\u8bc6\u5e93")], ["feishu"]),
        (zh("\u6d41\u7a0b"), zh("\u5185\u90e8\u6d41\u7a0b\u4e0e\u5236\u5ea6\u529e\u7406"), [zh("\u6d41\u7a0b"), zh("\u5236\u5ea6"), zh("\u5ba1\u6279"), zh("\u62a5\u9500"), zh("\u5165\u804c")], ["feishu"]),
        (zh("\u9500\u552e"), zh("\u9500\u552e\u7ebf\u7d22\u8ddf\u8fdb\u4e0e\u5ba2\u6237\u6458\u8981"), [zh("\u9500\u552e"), zh("\u7ebf\u7d22"), zh("\u5ba2\u6237")], ["feishu"]),
        (zh("\u77e5\u8bc6\u5e93"), zh("\u4f01\u4e1a\u77e5\u8bc6\u5e93\u95ee\u7b54"), [zh("\u77e5\u8bc6\u5e93"), zh("\u95ee\u7b54"), zh("\u6587\u6863")], []),
        (zh("\u62db\u8058"), zh("\u62db\u8058\u7b5b\u9009\u4e0e\u9762\u8bd5\u8f85\u52a9"), [zh("\u62db\u8058"), zh("\u7b80\u5386"), zh("\u9762\u8bd5")], []),
        (zh("PPT"), zh("\u6f14\u793a\u6587\u7a3f\u4e0e PPT \u751f\u6210"), [zh("PPT"), zh("\u6f14\u793a"), zh("\u5199\u4f5c")], []),
        (zh("\u5199\u4f5c"), zh("\u6587\u7ae0\u4e0e\u8425\u9500\u6587\u6848\u751f\u6210"), [zh("\u5199\u4f5c"), zh("\u6587\u6848"), zh("\u8bdd\u9898")], []),
        (zh("Excel"), zh("\u8868\u683c\u5904\u7406\u4e0e\u6570\u636e\u5206\u6790"), [zh("Excel"), zh("\u8868\u683c"), zh("\u6570\u636e"), zh("\u56fe\u8868")], []),
        (zh("\u7f51\u9875"), zh("\u7f51\u9875\u751f\u6210\u4e0e\u5185\u5bb9\u7f16\u6392"), [zh("\u7f51\u9875"), zh("\u8bbe\u8ba1"), zh("\u6587\u6848")], []),
        (zh("\u80a1\u7968"), zh("\u80a1\u7968\u5206\u6790\u4e0e\u98ce\u9669\u63d0\u9192"), [zh("\u80a1"), zh("\u884c\u60c5"), zh("\u8d22\u62a5"), zh("\u516c\u544a"), zh("\u98ce\u9669")], []),
    ]

    matched = None
    best_score = 0
    for _, domain_value, words, connector_list in keyword_groups:
        score = sum(1 for word in words if word.lower() in lowered or word in text)
        if score > best_score:
            best_score = score
            matched = (domain_value, words, connector_list)

    if matched:
        domain, words, connectors = matched
    else:
        domain = text[:40] if len(text) <= 40 else text[:40] + "..."
        words = re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,8}", text)[:4] or [zh("\u4efb\u52a1"), zh("\u8ba1\u5212")]
        connectors = []

    skills = []
    for word in words:
        skill = slug_skill(word)
        if skill not in skills:
            skills.append(skill)
    if "task-planning" not in skills:
        skills.append("task-planning")
    if "quality-check" not in skills:
        skills.append("quality-check")
    skills = skills[:5]

    role = zh("\u4f60\u662f") + name + zh("\u3002")
    prompt = (
        role
        + zh("\u4f60\u9700\u8981\u6839\u636e\u7528\u6237\u76ee\u6807\u6267\u884c\u4efb\u52a1\uff0c\u5e94\u7528\u573a\u666f\u662f\uff1a") + domain + zh("\u3002")
        + zh("\u4f60\u5fc5\u987b\u5148\u7406\u89e3\u9700\u6c42\uff0c\u518d\u7ed9\u51fa\u7ed3\u6784\u5316\u7ed3\u679c\u3001\u4e0b\u4e00\u6b65\u5efa\u8bae\u548c\u98ce\u9669\u63d0\u9192\u3002")
        + zh("\u5982\u679c\u4fe1\u606f\u4e0d\u8db3\uff0c\u8981\u4e3b\u52a8\u8bf4\u660e\u7f3a\u53e3\uff0c\u4e0d\u80fd\u7f16\u9020\u4e8b\u5b9e\u3002")
    )
    sample_input = text
    return {"name": name, "domain": domain, "prompt": prompt, "skills": skills, "connectors": connectors, "sample_input": sample_input}

def generated_main(agent_name: str, prompt: str, domain: str, skills: list[str]) -> str:
    return f'''SYSTEM_PROMPT = {prompt!r}
AGENT_NAME = {agent_name!r}
DOMAIN = {domain!r}
SKILLS = {skills!r}


def has_any(text: str, words: list[str]):
    lowered = text.lower()
    return any(word.lower() in lowered for word in words)


def main(input_text: str):
    context = " ".join(SKILLS).lower() + " " + DOMAIN.lower() + " " + input_text.lower()
    if has_any(context, ["stock", "market", "financial", "risk"]):
        return "\\n".join([
            AGENT_NAME + " local fallback: stock analysis mode.",
            "Question: " + input_text,
            "",
            "I cannot provide an unsupported buy recommendation, but I can provide a screening framework:",
            "1. Define holding period and risk tolerance.",
            "2. Exclude companies with major financial, regulatory, or announcement risks.",
            "3. Compare revenue growth, cash flow, valuation, debt, and industry trend.",
            "4. Provide stock codes or a watchlist for deeper comparison.",
            "",
            "Risk notice: this is analytical support, not investment advice.",
        ])
    if has_any(context, ["ppt", "presentation"]):
        return "\\n".join([
            AGENT_NAME + " local fallback: presentation mode.",
            "Topic: " + input_text,
            "Suggested pages: cover, background, goals, solution, value, next steps.",
        ])
    if has_any(context, ["customer", "ticket", "service"]):
        return "\\n".join([
            AGENT_NAME + " local fallback: customer service mode.",
            "Issue: " + input_text,
            "Suggested flow: confirm facts, match policy, provide next step, escalate sensitive cases.",
        ])
    return "\\n".join([
        AGENT_NAME + " local fallback response.",
        "Task: " + input_text,
        "Next: provide more data or constraints for a more specific answer.",
    ])


if __name__ == "__main__":
    print(main("test task"))
'''

def generated_skill(agent_name: str, domain: str, skills: list[str]) -> str:
    skill_lines = "\n".join(f"- {item}" for item in skills)
    title = zh("\\u5e94\\u7528\\u76ee\\u6807")
    ability = zh("\\u80fd\\u529b\\u7f16\\u6392")
    rules = zh("\\u6295\\u653e\\u89c4\\u5219")
    rule_1 = zh("- \\u56de\\u7b54\\u9700\\u8981\\u7b80\\u6d01\\u3001\\u53ef\\u6267\\u884c\\uff0c\\u5e76\\u4fdd\\u6301\\u4f01\\u4e1a\\u8bed\\u6c14\\u3002")
    rule_2 = zh("- \\u9047\\u5230\\u4fe1\\u606f\\u4e0d\\u8db3\\u65f6\\uff0c\\u4e3b\\u52a8\\u8bf4\\u660e\\u7f3a\\u53e3\\uff0c\\u4e0d\\u7f16\\u9020\\u4f9d\\u636e\\u3002")
    rule_3 = zh("- \\u6d89\\u53ca\\u5ba2\\u6237\\u3001\\u5408\\u540c\\u3001\\u6743\\u9650\\u7b49\\u654f\\u611f\\u4fe1\\u606f\\u65f6\\uff0c\\u63d0\\u9192\\u4eba\\u5de5\\u590d\\u6838\\u3002")
    rule_4 = zh("- \\u4f18\\u5148\\u4f7f\\u7528\\u5df2\\u6388\\u6743\\u77e5\\u8bc6\\u5e93\\u548c\\u5de5\\u5177\\u8f93\\u51fa\\u7ed3\\u679c\\u3002")
    return f'''# {agent_name} Skill Spec

## {title}
{domain}

## {ability}
{skill_lines}

## {rules}
{rule_1}
{rule_2}
{rule_3}
{rule_4}
'''


@router.post("/generate")
def generate_agent_app(payload: GenerateAgentRequest, user: User = Depends(current_user), ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    blueprint = infer_agent_blueprint(payload.message, payload.project_name)
    agent = Agent(name=blueprint["name"], description=blueprint["domain"], kind="code",
                  model="qwen-turbo", workspace_id=ws, status="draft", created_by=user.id)
    db.add(agent)
    db.flush()  # 拿到 agent.id，用作工作区目录名 = draft_id
    draft_dir = DRAFT_ROOT / agent.id
    draft_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": blueprint["name"],
        "description": zh("\\u9762\\u5411") + blueprint["domain"] + zh("\\u7684 Atlas Agent \\u5e94\\u7528\\u8349\\u7a3f"),
        "entry": "main.py",
        "runtime": "python",
        "model": "qwen-turbo",
        "prompt": blueprint["prompt"],
        "knowledge_bases": [],
        "skills": blueprint["skills"],
        "connectors": blueprint["connectors"],
        "permissions": {"network": bool(blueprint["connectors"]), "secrets": [], "filesystem": "sandbox"},
    }
    (draft_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (draft_dir / "main.py").write_text(generated_main(blueprint["name"], blueprint["prompt"], blueprint["domain"], blueprint["skills"]), encoding="utf-8")
    (draft_dir / "SKILL.md").write_text(generated_skill(blueprint["name"], blueprint["domain"], blueprint["skills"]), encoding="utf-8")
    test_name = zh("\\u57fa\\u7840\\u8fd0\\u884c\\u9a8c\\u8bc1")
    (draft_dir / "tests.json").write_text(json.dumps([{"name": test_name, "input": blueprint["sample_input"], "expected": blueprint["name"]}], ensure_ascii=False, indent=2), encoding="utf-8")
    create_version(db, agent, snapshot_code_agent(draft_dir), label="draft", note="")
    db.commit()
    reply = (
        zh("\\u5df2\\u751f\\u6210 Atlas Agent \\u9879\\u76ee\\u8349\\u7a3f\\uff1a") + blueprint["name"] + "\n\n"
        + zh("\\u5df2\\u521b\\u5efa 4 \\u4e2a\\u53ef\\u8fd0\\u884c\\u6587\\u4ef6\\uff1amanifest.json\\u3001main.py\\u3001SKILL.md\\u3001tests.json") + "\n"
        + zh("\\u5e94\\u7528\\u573a\\u666f\\uff1a") + blueprint["domain"] + "\n"
        + zh("\\u4e0b\\u4e00\\u6b65\\uff1a\\u8fdb\\u5165 Web IDE \\u8c03\\u6574\\u4ee3\\u7801\\u3001\\u63a5\\u5165\\u77e5\\u8bc6\\u5e93\\u6216\\u8fd0\\u884c\\u6d4b\\u8bd5\\uff0c\\u7136\\u540e\\u53ef\\u4f5c\\u4e3a\\u5e94\\u7528\\u6295\\u653e\\u539f\\u578b\\u7ee7\\u7eed\\u5b8c\\u5584\\u3002")
    )
    return {"draft": {"id": agent.id, "name": blueprint["name"], "status": "draft"}, "reply": reply, "blueprint": blueprint, "files": ["manifest.json", "main.py", "SKILL.md", "tests.json"]}


@router.post("/drafts")
def create_app_draft(payload: CreateDraftRequest, user: User = Depends(current_user), ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    name = payload.name.strip() or "demo-agent-app"
    agent = Agent(name=name, description="", kind="code", model="qwen-turbo", workspace_id=ws, status="draft", created_by=user.id)
    db.add(agent)
    db.flush()
    draft_dir = DRAFT_ROOT / agent.id

    ensure_demo_files(draft_dir, name)
    create_version(db, agent, snapshot_code_agent(draft_dir), label="draft", note="")
    db.commit()

    return {
        "id": agent.id,
        "name": name,
        "status": "draft",
    }


@router.get("/drafts/{draft_id}/files")
def list_draft_files(draft_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    draft_dir = draft_dir_or_404(draft_id, ws, db)
    files = []

    for item in sorted(draft_dir.rglob("*")):
        if item.is_dir() or item.name.startswith(".atlas_"):
            continue

        relative_path = item.relative_to(draft_dir).as_posix()
        files.append({"path": relative_path, "name": item.name, "type": "file"})

    return files


@router.post("/drafts/{draft_id}/files")
def create_draft_file(draft_id: str, payload: CreateFileRequest, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    draft_dir = draft_dir_or_404(draft_id, ws, db)
    file_path = safe_path(draft_dir, payload.path)

    if file_path.exists():
        raise HTTPException(status_code=409, detail="文件已存在")

    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(payload.content, encoding="utf-8")

    return {"ok": True, "path": file_path.relative_to(draft_dir).as_posix()}


@router.get("/drafts/{draft_id}/files/content")
def get_draft_file(draft_id: str, path: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    draft_dir = draft_dir_or_404(draft_id, ws, db)
    file_path = safe_path(draft_dir, path)

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    return {"path": path, "content": file_path.read_text(encoding="utf-8")}


@router.put("/drafts/{draft_id}/files/content")
def save_draft_file(draft_id: str, payload: SaveFileRequest, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    draft_dir = draft_dir_or_404(draft_id, ws, db)
    file_path = safe_path(draft_dir, payload.path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(payload.content, encoding="utf-8")

    return {"ok": True}


@router.put("/drafts/{draft_id}/files/rename")
def rename_draft_file(draft_id: str, payload: RenameFileRequest, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    draft_dir = draft_dir_or_404(draft_id, ws, db)
    old_path = safe_path(draft_dir, payload.old_path)
    new_path = safe_path(draft_dir, payload.new_path)

    if not old_path.exists() or not old_path.is_file():
        raise HTTPException(status_code=404, detail="原文件不存在")

    if new_path.exists():
        raise HTTPException(status_code=409, detail="目标文件已存在")

    new_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.rename(new_path)

    return {"ok": True, "path": new_path.relative_to(draft_dir).as_posix()}


@router.delete("/drafts/{draft_id}/files")
def delete_draft_file(draft_id: str, path: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    draft_dir = draft_dir_or_404(draft_id, ws, db)
    file_path = safe_path(draft_dir, path)

    if file_path.name in {"main.py", "manifest.json"}:
        raise HTTPException(status_code=400, detail="核心文件不能删除")

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    file_path.unlink()

    return {"ok": True}


@router.get("/drafts/{draft_id}/manifest/validate")
def validate_draft_manifest(draft_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    draft_dir = draft_dir_or_404(draft_id, ws, db)
    manifest, errors, warnings = validate_manifest(draft_dir)

    return {
        "ok": not errors,
        "manifest": manifest,
        "errors": errors,
        "warnings": warnings,
    }


@router.post("/drafts/{draft_id}/run")
async def run_draft_app(draft_id: str, payload: RunDraftRequest | None = None, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    draft_dir = draft_dir_or_404(draft_id, ws, db)
    input_text = payload.input_text if payload else "test input"

    model_result = await run_model_entry(draft_dir, input_text)
    if model_result and model_result.get("ok"):
        record_eval_run(db, draft_id, "test", model_result.get("ok", False), 1 if model_result.get("ok") else 0, 1, [model_result])
        return model_result

    fallback = run_python_entry(draft_dir, input_text)
    if model_result and model_result.get("error"):
        fallback["warnings"] = [*(fallback.get("warnings") or []), *(model_result.get("warnings") or [])]
    record_eval_run(db, draft_id, "test", fallback.get("ok", False), 1 if fallback.get("ok") else 0, 1, [fallback])
    return fallback


@router.post("/drafts/{draft_id}/evaluate")
def evaluate_draft_app(draft_id: str, payload: EvaluateDraftRequest, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    draft_dir = draft_dir_or_404(draft_id, ws, db)
    cases = payload.cases

    if not cases:
        tests_file = draft_dir / "tests.json"
        if tests_file.exists():
            try:
                raw_cases = json.loads(tests_file.read_text(encoding="utf-8"))
                cases = [TestCase(**item) for item in raw_cases]
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"tests.json 格式错误：{exc}") from exc

    if not cases:
        raise HTTPException(status_code=400, detail="请至少提供一个评测样例")

    results = []
    passed = 0

    for item in cases:
        run = run_python_entry(draft_dir, item.input)
        output = run["logs"].split("\n")[-2].strip() if run.get("ok") else ""
        ok = bool(run.get("ok")) and (not item.expected or item.expected in run.get("logs", ""))
        passed += 1 if ok else 0
        result = {
            "name": item.name,
            "input": item.input,
            "expected": item.expected,
            "ok": ok,
            "output": output,
            "logs": run.get("logs", ""),
            "elapsed_ms": run.get("elapsed_ms", 0),
        }
        if not ok:
            reason = classify_failure(run.get("ok", False), run.get("error"), item.expected, run.get("logs", ""))
            result["failure_reason"] = reason
            result["suggestion"] = suggest_fix(reason)
        else:
            result["failure_reason"] = ""
            result["suggestion"] = ""
        results.append(result)

    record_eval_run(db, draft_id, "evaluate", passed == len(cases), passed, len(cases), results)

    manifest = read_manifest(draft_dir) if (draft_dir / "manifest.json").exists() else {}
    declared_skills = manifest.get("skills") if isinstance(manifest.get("skills"), list) else []
    declared_connectors = manifest.get("connectors") if isinstance(manifest.get("connectors"), list) else []
    avg_elapsed_ms = sum(r["elapsed_ms"] for r in results) / len(results) if results else 0.0
    summary = build_evaluation_summary(passed / len(cases), avg_elapsed_ms, declared_skills, declared_connectors)

    return {
        "ok": passed == len(cases),
        "passed": passed,
        "total": len(cases),
        "pass_rate": round(passed / len(cases), 4),
        "results": results,
        "summary": summary,
    }


# ============ Agents + 版本管理 ============

agents_router = APIRouter(prefix="/api/agents", tags=["agent-versions"])


def version_or_404(agent_id: str, version_id: str, ws: str, db: Session) -> AgentVersion:
    agent_or_404(agent_id, ws, db)
    version = db.scalar(select(AgentVersion).where(AgentVersion.id == version_id, AgentVersion.agent_id == agent_id))
    if not version:
        raise HTTPException(status_code=404, detail="版本不存在")
    return version


def version_snapshot(version: AgentVersion) -> dict:
    try:
        return json.loads(version.snapshot_json)
    except json.JSONDecodeError:
        return {}


def diff_code_snapshots(old: dict, new: dict) -> list[dict]:
    old_files = old.get("files", {}) if isinstance(old, dict) else {}
    new_files = new.get("files", {}) if isinstance(new, dict) else {}
    paths = sorted(set(old_files) | set(new_files))
    files = []
    for path in paths:
        before = old_files.get(path)
        after = new_files.get(path)
        if before is None:
            files.append({"path": path, "status": "added", "diff": after or ""})
        elif after is None:
            files.append({"path": path, "status": "removed", "diff": before or ""})
        elif before != after:
            udiff = "\n".join(difflib.unified_diff(
                before.splitlines(), after.splitlines(),
                fromfile=path, tofile=path, lineterm="",
            ))
            files.append({"path": path, "status": "modified", "diff": udiff})
    return files


def diff_prompt_snapshots(old: dict, new: dict) -> list[dict]:
    fields = []
    for key in ("system_prompt", "model", "knowledge_base_id"):
        before = old.get(key) if isinstance(old, dict) else None
        after = new.get(key) if isinstance(new, dict) else None
        if before != after:
            fields.append({"field": key, "old": before, "new": after})
    return fields


@agents_router.get("/{agent_id}/versions", response_model=list[AgentVersionOut])
def list_agent_versions(agent_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    agent_or_404(agent_id, ws, db)
    return db.scalars(
        select(AgentVersion).where(AgentVersion.agent_id == agent_id).order_by(AgentVersion.version_no.desc())
    ).all()


@agents_router.post("/{agent_id}/versions", response_model=AgentVersionOut, status_code=201)
def save_agent_version(agent_id: str, payload: SaveVersionRequest, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    agent = agent_or_404(agent_id, ws, db)
    if agent.kind == "code":
        draft_dir = draft_dir_or_404(agent_id, ws, db)
        snapshot = snapshot_code_agent(draft_dir)
    else:
        snapshot = snapshot_prompt_agent(agent)
    version = create_version(db, agent, snapshot, label="history", note=payload.note)
    db.commit(); db.refresh(version)
    return version


@agents_router.get("/{agent_id}/versions/diff", response_model=AgentVersionDiff)
def diff_agent_versions(agent_id: str, from_: int = Query(alias="from"), to: int = Query(), ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    agent = agent_or_404(agent_id, ws, db)
    old_version = db.scalar(select(AgentVersion).where(AgentVersion.agent_id == agent_id, AgentVersion.version_no == from_))
    new_version = db.scalar(select(AgentVersion).where(AgentVersion.agent_id == agent_id, AgentVersion.version_no == to))
    if not old_version or not new_version:
        raise HTTPException(status_code=404, detail="版本不存在")
    old_snapshot, new_snapshot = version_snapshot(old_version), version_snapshot(new_version)
    if agent.kind == "code":
        return AgentVersionDiff(from_version=from_, to_version=to, kind=agent.kind, files=diff_code_snapshots(old_snapshot, new_snapshot))
    return AgentVersionDiff(from_version=from_, to_version=to, kind=agent.kind, fields=diff_prompt_snapshots(old_snapshot, new_snapshot))


@agents_router.get("/{agent_id}/versions/{version_id}", response_model=AgentVersionDetail)
def get_agent_version(agent_id: str, version_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    version = version_or_404(agent_id, version_id, ws, db)
    return AgentVersionDetail(
        id=version.id, agent_id=version.agent_id, version_no=version.version_no, kind=version.kind,
        label=version.label, note=version.note, created_at=version.created_at, snapshot=version_snapshot(version),
    )


@agents_router.post("/{agent_id}/versions/{version_id}/rollback", response_model=AgentVersionOut)
def rollback_agent_version(agent_id: str, version_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    agent = agent_or_404(agent_id, ws, db)
    target = version_or_404(agent_id, version_id, ws, db)
    snapshot = version_snapshot(target)
    if agent.kind == "code":
        draft_dir = draft_dir_or_404(agent_id, ws, db)
        write_workspace_files(draft_dir, snapshot.get("files", {}))
    else:
        agent.system_prompt = snapshot.get("system_prompt", agent.system_prompt)
        agent.model = snapshot.get("model", agent.model)
        agent.knowledge_base_id = snapshot.get("knowledge_base_id")
    new_version = create_version(db, agent, snapshot, label="history", note=f"回滚自 v{target.version_no}")
    db.commit(); db.refresh(new_version)
    return new_version


def checklist_for_agent(agent: Agent, db: Session) -> dict:
    deploy_config_configured = agent.deploy_config_json != "{}"
    latest_test = latest_eval_run(db, agent.id, "test")
    latest_eval = latest_eval_run(db, agent.id, "evaluate")
    deploy_config = parse_deploy_config(agent.deploy_config_json)
    return build_publish_checklist(
        kind=agent.kind,
        has_current_version=bool(agent.current_version_id),
        has_passed_test=bool(latest_test and latest_test.ok),
        last_eval_ok=(latest_eval.ok if latest_eval else None),
        deploy_config_configured=deploy_config_configured,
        allowed_connectors=deploy_config.get("allowed_connectors") or [],
    )


@agents_router.get("/{agent_id}/publish-checklist", response_model=PublishChecklistOut)
def get_publish_checklist(agent_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    agent = agent_or_404(agent_id, ws, db)
    return checklist_for_agent(agent, db)


@agents_router.post("/{agent_id}/publish", response_model=AgentVersionOut)
def publish_agent(agent_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    agent = agent_or_404(agent_id, ws, db)
    if not agent.current_version_id:
        raise HTTPException(status_code=400, detail="还没有可发布的版本")
    checklist = checklist_for_agent(agent, db)
    if not checklist["can_publish"]:
        missing = [item["label"] for item in checklist["items"] if item["level"] == "blocking" and not item["ok"]]
        raise HTTPException(status_code=403, detail=f"发布前检查未通过：{'、'.join(missing)}")
    current = db.get(AgentVersion, agent.current_version_id)
    if not current:
        raise HTTPException(status_code=400, detail="当前版本不存在")
    published = create_version(db, agent, version_snapshot(current), label="published", note=f"发布自 v{current.version_no}")
    agent.published_version_id = published.id
    agent.status = "published"
    db.commit(); db.refresh(published)
    return published


@agents_router.post("/{agent_id}/archive", response_model=AgentOut)
def archive_agent(agent_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    """下架：已发布的 Agent 停止对外可用（聊天入口和 /invoke 均拒绝），内容和版本历史不受影响，可随时重新上架。"""
    agent = agent_or_404(agent_id, ws, db)
    if agent.status != "published":
        raise HTTPException(status_code=400, detail="只有已发布的智能体才能下架")
    agent.status = "archived"
    db.commit(); db.refresh(agent)
    return AgentOut.model_validate(agent).model_copy(update=agent_out_fields(agent, db))


@agents_router.post("/{agent_id}/unarchive", response_model=AgentOut)
def unarchive_agent(agent_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    """重新上架：恢复为已发布状态，沿用原发布版本，不重新走发布前检查（内容未变）。"""
    agent = agent_or_404(agent_id, ws, db)
    if agent.status != "archived":
        raise HTTPException(status_code=400, detail="只有已下架的智能体才能重新上架")
    agent.status = "published"
    db.commit(); db.refresh(agent)
    return AgentOut.model_validate(agent).model_copy(update=agent_out_fields(agent, db))


# ============ 落地配置（PRD §5.7） ============

@agents_router.get("/{agent_id}/deploy-config", response_model=DeployConfigOut)
def get_deploy_config(agent_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    agent = agent_or_404(agent_id, ws, db)
    return DeployConfigOut(**parse_deploy_config(agent.deploy_config_json))


@agents_router.put("/{agent_id}/deploy-config", response_model=DeployConfigOut)
def update_deploy_config(agent_id: str, payload: DeployConfigUpdate, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    agent = agent_or_404(agent_id, ws, db)
    agent.deploy_config_json = json.dumps(payload.model_dump(), ensure_ascii=False)
    db.commit()
    return payload


@agents_router.get("/{agent_id}/call-logs", response_model=list[CallLogEntryOut])
def list_agent_call_logs(agent_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    agent = agent_or_404(agent_id, ws, db)
    deploy_config = parse_deploy_config(agent.deploy_config_json)
    if not deploy_config.get("call_log_enabled", True):
        return []
    runs = db.scalars(
        select(WorkflowRun).where(WorkflowRun.agent_id == agent_id).order_by(WorkflowRun.created_at.desc()).limit(200)
    ).all()
    entries = []
    for run in runs:
        latency_ms = None
        if run.started_at and run.ended_at:
            latency_ms = int((run.ended_at - run.started_at).total_seconds() * 1000)
        entries.append(CallLogEntryOut(
            id=run.id, time=run.created_at, source=run.source, status=run.status,
            latency_ms=latency_ms, error=run.error,
        ))
    return entries


# ============ API Key（PRD §6） ============

def _api_key_out(key: AgentApiKey | None, cls=ApiKeyOut, plaintext: str | None = None) -> ApiKeyOut:
    if not key:
        return cls(exists=False)
    fields = dict(
        exists=True, key_prefix=key.key_prefix, status=key.status, expires_at=key.expires_at,
        daily_quota=key.daily_quota, allowed_origins=key.allowed_origins,
        created_at=key.created_at, last_used_at=key.last_used_at,
    )
    if plaintext is not None:
        fields["key"] = plaintext
    return cls(**fields)


@agents_router.get("/{agent_id}/api-key", response_model=ApiKeyOut)
def get_api_key(agent_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    agent_or_404(agent_id, ws, db)
    key = db.scalar(select(AgentApiKey).where(AgentApiKey.agent_id == agent_id))
    return _api_key_out(key)


@agents_router.post("/{agent_id}/api-key", response_model=ApiKeyCreateOut, status_code=201)
def create_or_reset_api_key(agent_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    """创建或重置：一个 Agent 只有一条活跃记录，重置直接作废旧的（同一行更新，不留历史行）。"""
    agent_or_404(agent_id, ws, db)
    plaintext = generate_key()
    key = db.scalar(select(AgentApiKey).where(AgentApiKey.agent_id == agent_id))
    if not key:
        key = AgentApiKey(agent_id=agent_id)
        db.add(key)
    key.key_hash = hash_key(plaintext)
    key.key_prefix = display_prefix(plaintext)
    key.status = "active"
    key.last_used_at = None
    db.commit(); db.refresh(key)
    return _api_key_out(key, cls=ApiKeyCreateOut, plaintext=plaintext)


@agents_router.put("/{agent_id}/api-key", response_model=ApiKeyOut)
def update_api_key(agent_id: str, payload: ApiKeyUpdate, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    agent_or_404(agent_id, ws, db)
    key = db.scalar(select(AgentApiKey).where(AgentApiKey.agent_id == agent_id))
    if not key:
        raise HTTPException(status_code=404, detail="尚未生成 API Key")
    key.status = payload.status
    key.expires_at = payload.expires_at
    key.daily_quota = payload.daily_quota
    key.allowed_origins = payload.allowed_origins
    db.commit(); db.refresh(key)
    return _api_key_out(key)


@agents_router.delete("/{agent_id}/api-key", status_code=204)
def delete_api_key(agent_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    agent_or_404(agent_id, ws, db)
    key = db.scalar(select(AgentApiKey).where(AgentApiKey.agent_id == agent_id))
    if key:
        db.delete(key); db.commit()


# ============ Agent 模板 + 复制创建（PRD §5.2） ============

@agents_router.get("/templates", response_model=list[AgentTemplateOut])
def list_agent_templates():
    """内置模板目录（本轮最小实现，见 agent_templates.py）：只给前端展示 id/name/description，不暴露 system_prompt。"""
    return [AgentTemplateOut(id=t["id"], name=t["name"], description=t["description"]) for t in TEMPLATES]


@agents_router.post("/from-template", response_model=AgentOut, status_code=201)
def create_agent_from_template(payload: CreateFromTemplateRequest, user: User = Depends(current_user), ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    template = get_template(payload.template_id)
    if not template:
        raise HTTPException(status_code=404, detail="模板不存在")
    agent = Agent(name=template["name"], description=template["description"], kind="prompt",
                  system_prompt=template["system_prompt"], model="qwen-turbo", workspace_id=ws, status="draft", created_by=user.id)
    db.add(agent)
    db.flush()
    create_version(db, agent, snapshot_prompt_agent(agent), label="draft", note=f"来自模板：{template['name']}")
    db.commit(); db.refresh(agent)
    return AgentOut.model_validate(agent).model_copy(update=agent_out_fields(agent, db))


@agents_router.post("/{agent_id}/copy", response_model=AgentOut, status_code=201)
def copy_agent(agent_id: str, user: User = Depends(current_user), ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    source = agent_or_404(agent_id, ws, db)
    copy = Agent(name=f"{source.name}（副本）", description=source.description, kind=source.kind,
                 system_prompt=source.system_prompt, model=source.model, knowledge_base_id=source.knowledge_base_id,
                 workspace_id=ws, status="draft", created_by=user.id)
    db.add(copy)
    db.flush()  # 拿 copy.id：kind=code 时用作新工作区目录名
    if source.kind == "code":
        source_dir = draft_dir_or_404(source.id, ws, db)
        target_dir = DRAFT_ROOT / copy.id
        target_dir.mkdir(parents=True, exist_ok=True)
        write_workspace_files(target_dir, read_workspace_files(source_dir))
        create_version(db, copy, snapshot_code_agent(target_dir), label="draft", note=f"复制自 {source.name}")
    else:
        create_version(db, copy, snapshot_prompt_agent(copy), label="draft", note=f"复制自 {source.name}")
    db.commit(); db.refresh(copy)
    return AgentOut.model_validate(copy).model_copy(update=agent_out_fields(copy, db))
