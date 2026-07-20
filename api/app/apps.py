from pathlib import Path
from uuid import uuid4
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
import difflib
import json
import re
import subprocess
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
from .model_gateway import complete, demo_answer, embed_query, resolve_provider, stream_agent
from .models import Agent, AgentApiKey, AgentEvalRun, AgentVersion, EvaluationSuite, KnowledgeBase, Membership, Skill, User, WorkflowRun
from .schemas import AgentOut, AgentTemplateOut, AgentVersionDetail, AgentVersionDiff, AgentVersionOut, ApiKeyCreateOut, ApiKeyOut, ApiKeyUpdate, CallLogEntryOut, CreateFromTemplateRequest, DeployConfigOut, DeployConfigUpdate, PublishChecklistOut, SaveVersionRequest, WorkspaceMemberOut
from .skills_engine import build_skill_instructions
from .connectors import github_mcp, remote_mcp
from . import tools as agent_tools
from . import workflow as wf
from .code_runner import run_python
from .config import settings
from .object_storage import agent_archive_key, object_storage


router = APIRouter(prefix="/api/apps", tags=["apps"])

BASE_DIR = Path(__file__).resolve().parent
DRAFT_ROOT = Path(settings.agent_code_root).resolve()


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
    confirmed_tools: list[str] = []


class TestCase(BaseModel):
    name: str
    input: str
    expected: str = ""


class EvaluateDraftRequest(BaseModel):
    cases: list[TestCase] = []


class GenerateAgentRequest(BaseModel):
    message: str
    project_name: str = ""


class RefineAgentRequest(BaseModel):
    message: str



DEFAULT_MAIN = '''from agent import AtlasAgent


def main(input_text: str):
    agent = AtlasAgent()
    return agent.run(input_text)


if __name__ == "__main__":
    print(main("test task"))
'''


FRAMEWORK_FILES = [
    "manifest.json",
    "main.py",
    "agent.py",
    "runtime.py",
    "skills.py",
    "knowledge.py",
    "connectors.py",
    "SKILL.md",
    "tests.json",
    "README.md",
]


def default_manifest(app_name: str) -> str:
    return json.dumps(
        {
            "name": app_name,
            "description": "企业内部智能应用草稿",
            "framework": "atlas-agent-python",
            "entry": "main.py",
            "runtime": "python",
            "model": "qwen-turbo",
            "prompt": "你是一个可靠的企业智能体，请根据输入给出清晰、可执行的回答。",
            "agent": {
                "module": "agent.py",
                "class": "AtlasAgent",
            },
            "pipeline": ["plan", "retrieve", "use_skills", "use_connectors", "compose", "guard"],
            "io_schema": {
                "input": {"type": "string", "name": "input_text"},
                "output": {"type": "string", "format": "markdown"},
            },
            "knowledge_bases": [],
            "skills": [],
            "connectors": [],
            "files": FRAMEWORK_FILES,
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
    try:
        object_storage.put(
            agent_archive_key(agent.workspace_id or "legacy", agent.id, version.version_no),
            json.dumps(snapshot, ensure_ascii=False).encode("utf-8"),
            "application/json",
        )
    except Exception:
        if settings.object_storage_required:
            raise
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
    deploy_configured = agent.deploy_config_json != "{}"
    has_unpublished_changes = False
    if agent.published_version_id:
        published_version = db.get(AgentVersion, agent.published_version_id)
        published_snapshot = version_snapshot(published_version) if published_version else {}
        if agent.kind == "code":
            draft_dir = DRAFT_ROOT / agent.id
            if draft_dir.exists():
                has_unpublished_changes = published_snapshot.get("files", {}) != read_workspace_files(draft_dir)
        else:
            live_prompt_snapshot = snapshot_prompt_agent(agent)
            has_unpublished_changes = any(
                live_prompt_snapshot.get(key) != published_snapshot.get(key)
                for key in ("system_prompt", "model", "knowledge_base_id")
            )
    if agent.status == "archived":
        workflow_stage = "archived"
    elif agent.status == "published":
        workflow_stage = "publish" if has_unpublished_changes else "published"
    elif not agent.current_version_id:
        workflow_stage = "develop"
    elif not latest_test or not latest_test.ok:
        workflow_stage = "test"
    elif not latest_eval:
        workflow_stage = "evaluate"
    elif not latest_eval.ok:
        workflow_stage = "risk"
    elif not deploy_configured:
        workflow_stage = "deploy"
    else:
        workflow_stage = "publish"

    if (latest_eval and not latest_eval.ok) or (latest_test and not latest_test.ok):
        health_status = "risk"
    elif agent.status == "published" and latest_eval and latest_eval.ok and not has_unpublished_changes:
        health_status = "healthy"
    elif latest_test or latest_eval:
        health_status = "attention"
    else:
        health_status = "unknown"
    success_rate = latest_eval.pass_rate if latest_eval else (latest_test.pass_rate if latest_test else None)

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
        "deploy_config_configured": deploy_configured,
        "workflow_stage": workflow_stage,
        "health_status": health_status,
        "success_rate": success_rate,
        "has_unpublished_changes": has_unpublished_changes,
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

    files = scaffold_agent_files(
        agent_name=app_name,
        prompt="你是一个可靠的企业智能体，请根据输入给出清晰、可执行的回答。",
        domain="企业内部智能应用草稿",
        skills=["task-planning", "quality-check"],
        connectors=[],
        sample_input="Atlas",
    )
    for path, content in files.items():
        target = draft_dir / path
        if not target.exists():
            target.write_text(content, encoding="utf-8")


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
        return {
            "ok": False,
            "logs": "\n".join([
                "> 模型供应商未配置 API Key，已切换到本地框架回退",
                f"> model: {real_model}",
            ]),
            "error": "missing_model_api_key",
            "warnings": ["模型供应商未配置 API Key，真实大模型运行不可用，当前使用本地框架回退"],
            "elapsed_ms": 0,
        }

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
        result = run_python(draft_dir, min(timeout, settings.code_runner_timeout_seconds))
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "logs": "> 沙箱运行超时：超过 10 秒",
            "error": "timeout",
            "warnings": warnings,
        }
    except (OSError, RuntimeError) as exc:
        return {
            "ok": False,
            "logs": f"> 隔离运行器不可用：{exc}",
            "error": "runner_unavailable",
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


def builder_messages_path(draft_dir: Path) -> Path:
    return draft_dir / ".atlas_builder_messages.json"


def read_builder_messages(draft_dir: Path) -> list[dict]:
    path = builder_messages_path(draft_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def append_builder_message(draft_dir: Path, role: str, content: str, **extra) -> dict:
    messages = read_builder_messages(draft_dir)
    item = {
        "id": str(uuid4()),
        "role": role,
        "content": content,
        "created_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
    messages.append(item)
    builder_messages_path(draft_dir).write_text(
        json.dumps(messages[-200:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return item


def runtime_package(agent: Agent, db: Session, use_published: bool) -> dict:
    """Return the immutable files/config used by one execution."""
    version: AgentVersion | None = None
    if use_published:
        if agent.status != "published" or not agent.published_version_id:
            raise HTTPException(status_code=400, detail="该 Agent 没有可运行的发布版本")
        version = db.get(AgentVersion, agent.published_version_id)
        if not version:
            raise HTTPException(status_code=400, detail="发布版本不存在")

    if agent.kind == "code":
        if version:
            snapshot = version_snapshot(version)
            files = snapshot.get("files", {}) if isinstance(snapshot, dict) else {}
            manifest = snapshot.get("manifest", {}) if isinstance(snapshot, dict) else {}
            if not manifest and isinstance(files, dict) and files.get("manifest.json"):
                try:
                    manifest = json.loads(files["manifest.json"])
                except json.JSONDecodeError:
                    manifest = {}
        else:
            draft_dir = draft_dir_or_404(agent.id, agent.workspace_id or "", db)
            files = read_workspace_files(draft_dir)
            manifest = read_manifest(draft_dir)
        return {
            "kind": "code",
            "files": files,
            "manifest": manifest,
            "version_no": version.version_no if version else None,
            "version_label": "published" if version else "draft",
        }

    snapshot = version_snapshot(version) if version else snapshot_prompt_agent(agent)
    prompt_deploy_config = parse_deploy_config(agent.deploy_config_json)
    return {
        "kind": "prompt",
        "files": {},
        "manifest": {
            "name": agent.name,
            "description": agent.description,
            "prompt": snapshot.get("system_prompt", agent.system_prompt),
            "model": snapshot.get("model", agent.model),
            "knowledge_bases": [snapshot.get("knowledge_base_id")] if snapshot.get("knowledge_base_id") else [],
            "skills": prompt_deploy_config.get("allowed_skill_ids") or [],
            "connectors": prompt_deploy_config.get("allowed_connectors") or [],
        },
        "version_no": version.version_no if version else None,
        "version_label": "published" if version else "draft",
    }


def run_snapshot_python(files: dict[str, str], input_text: str) -> dict:
    with TemporaryDirectory(prefix="atlas-agent-") as temp_dir:
        root = Path(temp_dir)
        write_workspace_files(root, files)
        return run_python_entry(root, input_text)


async def runtime_tooling(connectors: list[str]) -> tuple[list[dict], dict[str, str], set[str], list[str]]:
    specs = agent_tools.specs(connectors)
    owners = {name: item["connector"] for name, item in agent_tools.TOOLS.items()}
    writes = {spec["function"]["name"] for spec in specs if agent_tools.is_write(spec["function"]["name"])}
    warnings: list[str] = []

    if "github" in connectors:
        if github_mcp.is_configured():
            try:
                items, write_names = await github_mcp.tool_specs()
                specs.extend(items)
                writes |= write_names
                owners.update({item["function"]["name"]: "github" for item in items})
            except Exception as exc:
                warnings.append(f"GitHub 连接器不可用：{exc}")
        else:
            warnings.append("GitHub 连接器尚未配置")

    for provider in connectors:
        if provider in {"github", "feishu"} or not remote_mcp.is_known(provider):
            continue
        if not remote_mcp.is_configured(provider):
            warnings.append(f"{provider} 连接器尚未配置")
            continue
        try:
            items, write_names = await remote_mcp.tool_specs(provider)
            specs.extend(items)
            writes |= write_names
            owners.update({item["function"]["name"]: provider for item in items})
        except Exception as exc:
            warnings.append(f"{provider} 连接器不可用：{exc}")
    return specs, owners, writes, warnings


def _runtime_actor_id(agent: Agent, db: Session, explicit_user_id: str | None) -> str | None:
    """Resolve a real tenant member for durable runtime attribution.

    External API and background evaluation calls do not carry a browser user,
    but RuntimeRun deliberately requires an actor FK. Prefer the authenticated
    user, then the Agent creator, then a deterministic workspace owner/member.
    """
    if explicit_user_id:
        return explicit_user_id
    if agent.created_by:
        return agent.created_by
    if not agent.workspace_id:
        return None
    return db.scalar(
        select(Membership.user_id)
        .where(Membership.workspace_id == agent.workspace_id)
        .order_by((Membership.role == "owner").desc(), Membership.created_at, Membership.user_id)
    )


async def _execute_langgraph_runtime(
    agent: Agent,
    input_text: str,
    db: Session,
    *,
    use_published: bool,
    source: str,
    conversation_id: str | None,
    user_id: str | None,
    package: dict,
) -> dict | None:
    """Compatibility facade used by every legacy product entry point.

    It returns ``None`` only when the caller has no immutable prompt version;
    callers may use the legacy adapter before any runtime side effect when the
    explicit fallback flag permits it. Code Agents remain on the Docker sandbox
    adapter until their executable graph node is introduced in the next phase.
    """
    if agent.kind != "prompt":
        return None
    version_id = agent.published_version_id if use_published else (
        agent.current_version_id or agent.published_version_id
    )
    actor_id = _runtime_actor_id(agent, db, user_id)
    if not version_id or not actor_id or not agent.workspace_id:
        return None
    version = db.scalar(select(AgentVersion).where(
        AgentVersion.id == version_id,
        AgentVersion.agent_id == agent.id,
    ))
    if version is None:
        return None

    from .runtime_api import RuntimeRunCreate, _start_service, _to_start_request
    from .runtime_contract import RuntimeSource, RuntimeStatus

    source_map = {
        "chat": RuntimeSource.CHAT,
        "workbench": RuntimeSource.WORKBENCH,
        "preview": RuntimeSource.PREVIEW,
        "builder": RuntimeSource.BUILDER,
        "evaluate": RuntimeSource.EVALUATION,
        "evaluation": RuntimeSource.EVALUATION,
        "api": RuntimeSource.API,
        "subagent": RuntimeSource.SUBAGENT,
    }
    runtime_source = source_map.get(source, RuntimeSource.PREVIEW)
    snapshot = version_snapshot(version)
    started = time.perf_counter()
    payload = RuntimeRunCreate(
        agent_id=agent.id,
        input=input_text,
        idempotency_key=f"compat:{runtime_source.value}:{uuid4()}",
        source=runtime_source,
        version_id=version.id,
        conversation_id=conversation_id,
    )
    async with _start_service(snapshot, agent.workspace_id) as service:
        handle = await service.start(_to_start_request(
            payload,
            workspace_id=agent.workspace_id,
            user_id=actor_id,
            version_id=version.id,
        ))
        handle = await service.wait(run_id=handle.run_id, workspace_id=agent.workspace_id)
        record = service.runs.get(run_id=handle.run_id, workspace_id=agent.workspace_id)
        events = service.stream(run_id=handle.run_id, workspace_id=agent.workspace_id)

    state = record.state
    answer = state.output or ""
    active_errors = [item for item in state.errors if not item.get("recovered")]
    error = str(active_errors[-1].get("message")) if active_errors else None
    trace = [
        {
            "type": event.payload.get("node", event.type),
            "title": event.payload.get("node", event.type),
            "status": event.payload.get("status", "failed" if event.type == "node.failed" else "succeeded"),
            "executor": "langgraph",
            **({"error": event.payload.get("error", "")} if event.type == "node.failed" else {}),
        }
        for event in events
        if event.type in {"node.completed", "node.failed"}
    ]
    tool_calls = [
        {"name": item.get("name", ""), "status": "succeeded", "access": "read"}
        for item in state.tool_results
    ]
    sources: list[dict] = []
    for item in state.tool_results:
        result = item.get("result")
        if item.get("name") == "knowledge_search" and isinstance(result, dict):
            sources.extend(result.get("matches") or [])
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    ok = handle.status is RuntimeStatus.SUCCEEDED
    warnings = []
    logs = "\n".join([
        f"> runtime: {record.execution_mode}",
        f"> version: {package['version_label']} {package['version_no'] or 'workspace'}",
        f"> run_id: {handle.run_id}",
        f"> elapsed: {elapsed_ms} ms",
        "",
        answer or error or "",
    ])
    return {
        "ok": ok,
        "answer": answer,
        "content": answer,
        "logs": logs,
        "error": error,
        "warnings": warnings,
        "sources": sources,
        "trace": trace,
        "tool_calls": tool_calls,
        "requires_confirmation": None,
        "elapsed_ms": elapsed_ms,
        "usage": None,
        "run_id": handle.run_id,
        "version_no": package["version_no"],
        "version_label": package["version_label"],
        "runtime_mode": record.execution_mode,
    }


async def execute_agent_runtime(
    agent: Agent,
    input_text: str,
    db: Session,
    *,
    use_published: bool = False,
    source: str = "preview",
    conversation_id: str | None = None,
    user_id: str | None = None,
    confirmed_tools: list[str] | None = None,
    record_log_override: bool | None = None,
) -> dict:
    """One runtime for draft preview, workbench, evaluation and external API."""
    started = time.perf_counter()
    package = runtime_package(agent, db, use_published)
    if settings.langgraph_runtime_enabled:
        unified = await _execute_langgraph_runtime(
            agent,
            input_text,
            db,
            use_published=use_published,
            source=source,
            conversation_id=conversation_id,
            user_id=user_id,
            package=package,
        )
        if unified is not None:
            return unified
        if not settings.langgraph_runtime_legacy_fallback:
            raise HTTPException(
                status_code=409,
                detail="该入口缺少可绑定的不可变 Prompt Agent 版本，禁止绕过统一运行时",
            )
    manifest = package["manifest"] if isinstance(package.get("manifest"), dict) else {}
    model = str(manifest.get("model") or agent.model or "qwen-turbo")
    prompt = str(manifest.get("prompt") or agent.system_prompt)
    deploy_config = parse_deploy_config(agent.deploy_config_json)
    record_log = bool(deploy_config.get("call_log_enabled", True)) if record_log_override is None else record_log_override
    run_id = wf.create_run(conversation_id, agent.id, user_id, agent.workspace_id or "", input_text, source=source) if record_log else None
    trace: list[dict] = []
    warnings: list[str] = []

    def start_step(step_type: str, title: str, executor: str, input_data: dict | None = None) -> tuple[str | None, dict]:
        item = {"type": step_type, "title": title, "status": "running", "executor": executor}
        trace.append(item)
        step_id = wf.add_step(run_id, len(trace) - 1, step_type, title, executor, input_data=input_data) if run_id else None
        return step_id, item

    def finish_step(step_id: str | None, item: dict, status: str, output: dict | None = None, error: str = "") -> None:
        item["status"] = status
        if output is not None:
            item["output"] = output
        if error:
            item["error"] = error
        if step_id:
            wf.finish_step(step_id, status, output=output, error=error)

    knowledge_ids = [str(item) for item in (manifest.get("knowledge_bases") or []) if item]
    skill_refs = [str(item) for item in (manifest.get("skills") or []) if item]
    connectors = [str(item) for item in (manifest.get("connectors") or []) if item]
    if use_published:
        allowed_kb = deploy_config.get("allowed_knowledge_base_ids") or []
        allowed_skills = deploy_config.get("allowed_skill_ids") or []
        allowed_connectors = deploy_config.get("allowed_connectors") or []
        if allowed_kb:
            knowledge_ids = [item for item in knowledge_ids if item in allowed_kb]
        if allowed_connectors:
            connectors = [item for item in connectors if item in allowed_connectors]
    else:
        allowed_skills = []

    sources: list[dict] = []
    knowledge_blocks: list[str] = []
    if knowledge_ids:
        step_id, item = start_step("retrieve", "检索已授权知识库", "rag", {"knowledge_base_ids": knowledge_ids})
        query_vector = await embed_query(input_text)
        owned_ids = set(db.scalars(select(KnowledgeBase.id).where(
            KnowledgeBase.workspace_id == agent.workspace_id,
            KnowledgeBase.id.in_(knowledge_ids),
        )).all())
        for kb_id in knowledge_ids:
            if kb_id not in owned_ids:
                warnings.append(f"知识库 {kb_id} 不存在或无权访问")
                continue
            for hit in search_chunks(db, kb_id, input_text, query_vector)[:4]:
                source_item = {key: hit.get(key) for key in ("document", "page", "quote", "score")}
                source_item["knowledge_base_id"] = kb_id
                sources.append(source_item)
                knowledge_blocks.append(f"[{hit['document']} 第{hit['page']}页]\n{hit['content']}")
        finish_step(step_id, item, "succeeded", {"hits": len(sources)})

    skill_rows = db.scalars(select(Skill).where(Skill.workspace_id == agent.workspace_id, Skill.status == "active")).all()
    selected_skills = []
    for skill in skill_rows:
        if skill.id not in skill_refs and skill.name not in skill_refs:
            continue
        if use_published and allowed_skills and skill.id not in allowed_skills:
            continue
        selected_skills.append({"id": skill.id, "name": skill.name, "content": skill.content, "source": "manual"})
    project_skill = package.get("files", {}).get("SKILL.md") if package["kind"] == "code" else ""
    if project_skill and skill_refs:
        selected_skills.insert(0, {
            "id": "project-skill",
            "name": "项目内置 Skill",
            "content": project_skill,
            "source": "project",
        })
    skill_text = build_skill_instructions(selected_skills)
    if skill_refs:
        step_id, item = start_step("skill", "加载 Agent Skills", "skill_agent", {"skills": skill_refs})
        finish_step(step_id, item, "succeeded", {"loaded": [item["name"] for item in selected_skills]})

    base_url, api_key, real_model = resolve_provider(model)
    if not api_key:
        if package["kind"] == "code":
            if use_published:
                fallback = run_snapshot_python(package["files"], input_text)
            else:
                fallback = run_python_entry(draft_dir_or_404(agent.id, agent.workspace_id or "", db), input_text)
            answer = fallback.get("content", "")
            ok = bool(fallback.get("ok"))
            error = fallback.get("error")
            logs = fallback.get("logs", "")
        else:
            history = [{"role": "system", "content": prompt}]
            if knowledge_blocks:
                history.append({"role": "system", "content": "知识库资料：\n" + "\n\n".join(knowledge_blocks)})
            history.append({"role": "user", "content": input_text})
            answer = demo_answer(history)
            ok, error, logs = True, None, answer
        warnings.append(f"模型 {real_model} 未配置 API Key，当前使用本地回退运行")
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if run_id:
            wf.finish_run(run_id, "succeeded" if ok else "failed", output={"answer": answer, "sources": sources, "warnings": warnings}, error=error or "")
        return {
            "ok": ok, "answer": answer, "content": answer, "logs": logs, "error": error,
            "warnings": warnings, "sources": sources, "trace": trace, "tool_calls": [],
            "elapsed_ms": elapsed_ms, "usage": None, "run_id": run_id,
            "version_no": package["version_no"], "version_label": package["version_label"],
        }

    specs, owners, write_names, tool_warnings = await runtime_tooling(connectors)
    warnings.extend(tool_warnings)
    messages = [{"role": "system", "content": prompt}]
    if knowledge_blocks:
        messages.append({"role": "system", "content": "仅依据以下已授权知识库资料回答需要事实依据的问题：\n\n" + "\n\n".join(knowledge_blocks)})
    if skill_text:
        messages.append({"role": "system", "content": skill_text})
    messages.append({"role": "user", "content": input_text})
    confirmed = set(confirmed_tools or [])
    tool_calls: list[dict] = []
    answer_parts: list[str] = []
    pending_confirmation: dict | None = None

    async def execute_tool(name: str, arguments: str) -> str:
        owner = owners.get(name, "")
        if owner == "github":
            return await github_mcp.call_tool(name, arguments)
        if owner and owner not in {"feishu"} and remote_mcp.is_known(owner):
            return await remote_mcp.call_tool(owner, name, arguments)
        return await agent_tools.dispatch(db, name, arguments)

    respond_id, respond_item = start_step("respond", "执行 Agent 并生成回答", "llm", {"model": real_model})
    try:
        async for event in stream_agent(
            messages,
            model,
            specs,
            execute_tool,
            needs_confirm=lambda name: name in write_names and name not in confirmed,
        ):
            if event["type"] == "token":
                answer_parts.append(event.get("content", ""))
            elif event["type"] == "tool_call":
                tool_calls.append({"name": event.get("name", ""), "status": "running", "provider": owners.get(event.get("name", ""), "")})
            elif event["type"] == "tool_result" and tool_calls:
                tool_calls[-1]["status"] = "succeeded"
            elif event["type"] == "confirm_required":
                pending_confirmation = {
                    "tool": event.get("name", ""),
                    "arguments": event.get("args", ""),
                    "provider": owners.get(event.get("name", ""), ""),
                }
        answer = "".join(answer_parts).strip()
        if pending_confirmation and not answer:
            answer = f"该任务需要执行写操作 {pending_confirmation['tool']}，请确认后继续。"
        finish_step(respond_id, respond_item, "waiting_confirmation" if pending_confirmation else "succeeded", {
            "answer_preview": answer[:500], "tool_calls": tool_calls,
        })
        ok = not pending_confirmation
        error = "confirmation_required" if pending_confirmation else None
    except Exception as exc:
        answer, ok, error = "", False, str(exc)
        finish_step(respond_id, respond_item, "failed", error=error)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if run_id:
        wf.finish_run(
            run_id,
            "waiting_confirmation" if pending_confirmation else ("succeeded" if ok else "failed"),
            output={"answer": answer, "sources": sources, "tool_calls": tool_calls, "warnings": warnings, "version_no": package["version_no"]},
            error=error or "",
        )
    logs = "\n".join([
        f"> version: {package['version_label']} {package['version_no'] or 'workspace'}",
        f"> model: {real_model}",
        f"> knowledge hits: {len(sources)}",
        f"> tools: {len(tool_calls)}",
        f"> elapsed: {elapsed_ms} ms",
        "",
        answer,
    ])
    return {
        "ok": ok, "answer": answer, "content": answer, "logs": logs, "error": error,
        "warnings": warnings, "sources": sources, "trace": trace, "tool_calls": tool_calls,
        "requires_confirmation": pending_confirmation, "elapsed_ms": elapsed_ms, "usage": None,
        "run_id": run_id, "version_no": package["version_no"], "version_label": package["version_label"],
    }


async def invoke_agent(agent: Agent, input_text: str, db: Session) -> dict:
    """External invocation always executes the immutable published version."""
    result = await execute_agent_runtime(
        agent,
        input_text,
        db,
        use_published=True,
        source="api",
        record_log_override=False,
    )
    return {
        "ok": result["ok"],
        "output": result.get("answer", ""),
        "error": result.get("error"),
        "elapsed_ms": result.get("elapsed_ms", 0),
        "version_no": result.get("version_no"),
        "run_id": result.get("run_id"),
    }




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
    needs_preview = any(word in lowered for word in ("html", "ui", "web", "frontend")) or any(
        word in text for word in (zh("\u7f51\u9875"), zh("\u9875\u9762"), zh("\u524d\u7aef"), zh("\u754c\u9762"))
    )
    return {
        "name": name,
        "domain": domain,
        "prompt": prompt,
        "skills": skills,
        "connectors": connectors,
        "sample_input": sample_input,
        "needs_preview": needs_preview,
    }

def generated_runtime() -> str:
    return '''from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentContext:
    input_text: str
    plan: list[str] = field(default_factory=list)
    knowledge: list[str] = field(default_factory=list)
    skill_notes: list[str] = field(default_factory=list)
    connector_results: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    answer: str
    next_steps: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def render(self) -> str:
        sections = [self.answer.strip()]
        if self.next_steps:
            sections.append("Next steps:\\n" + "\\n".join(f"- {item}" for item in self.next_steps))
        if self.risks:
            sections.append("Risks and checks:\\n" + "\\n".join(f"- {item}" for item in self.risks))
        if self.sources:
            sections.append("Sources:\\n" + "\\n".join(f"- {item}" for item in self.sources))
        return "\\n\\n".join(section for section in sections if section)
'''


def generated_skills_module(skills: list[str]) -> str:
    return f'''DEFAULT_SKILLS = {skills!r}


class SkillRouter:
    def __init__(self, skills=None):
        self.skills = list(skills or DEFAULT_SKILLS)

    def apply(self, input_text: str, context):
        if not self.skills:
            return ["No custom skills configured yet."]

        notes = []
        lowered = input_text.lower()
        for skill in self.skills:
            notes.append(self._run_skill(skill, lowered, context))
        return notes

    def _run_skill(self, skill: str, lowered_input: str, context):
        if skill in {{"quality-check", "quality_check"}}:
            return "quality-check: validate facts, constraints, sensitive data, and escalation needs."
        if skill in {{"task-planning", "task_planning"}}:
            return "task-planning: split the request into plan, action, review, and handoff."
        if "ppt" in skill or "presentation" in skill:
            return "presentation: produce page outline, key message, and speaker notes."
        if "customer" in skill or "service" in skill or "ticket" in skill:
            return "customer-service: confirm issue, match policy, and suggest escalation path."
        if "stock" in skill or "risk" in skill or "finance" in skill:
            return "finance-risk: provide analysis framework only; never promise returns."
        return f"{{skill}}: placeholder handler ready for implementation."
'''


def generated_knowledge_module() -> str:
    return '''class KnowledgeAdapter:
    def __init__(self, knowledge_bases=None):
        self.knowledge_bases = list(knowledge_bases or [])

    def search(self, input_text: str):
        if not self.knowledge_bases:
            return []
        return [
            f"{kb}: connect this placeholder to vector search or document retrieval."
            for kb in self.knowledge_bases
        ]
'''


def generated_connectors_module(connectors: list[str]) -> str:
    return f'''DEFAULT_CONNECTORS = {connectors!r}


class ConnectorRegistry:
    def __init__(self, connectors=None):
        self.connectors = list(connectors or DEFAULT_CONNECTORS)

    def call_all(self, input_text: str, context):
        results = []
        for connector in self.connectors:
            results.append(self.call(connector, {{"input_text": input_text}}))
        return results

    def call(self, name: str, payload: dict):
        return {{
            "connector": name,
            "status": "not_configured",
            "message": "Wire this adapter to the approved external system before production use.",
        }}
'''


def generated_agent(agent_name: str, prompt: str, domain: str, skills: list[str], connectors: list[str]) -> str:
    return f'''from connectors import ConnectorRegistry
from knowledge import KnowledgeAdapter
from runtime import AgentContext, AgentResult
from skills import SkillRouter


class AtlasAgent:
    name = {agent_name!r}
    domain = {domain!r}
    system_prompt = {prompt!r}

    def __init__(self, knowledge_bases=None, skills=None, connectors=None):
        self.knowledge = KnowledgeAdapter(knowledge_bases)
        self.skills = SkillRouter(skills or {skills!r})
        self.connectors = ConnectorRegistry(connectors or {connectors!r})

    def plan(self, input_text: str) -> list[str]:
        return [
            "Understand the user goal and missing constraints.",
            "Retrieve approved knowledge and connector context.",
            "Run domain skills and compose a structured answer.",
            "Check risks before returning the final response.",
        ]

    def run(self, input_text: str) -> str:
        context = AgentContext(input_text=input_text)
        context.plan = self.plan(input_text)
        context.knowledge = self.knowledge.search(input_text)
        context.skill_notes = self.skills.apply(input_text, context)
        connector_payloads = self.connectors.call_all(input_text, context)
        context.connector_results = [item["message"] for item in connector_payloads]
        result = self.compose(context)
        return result.render()

    def compose(self, context: AgentContext) -> AgentResult:
        answer_lines = [
            f"{{self.name}} is ready.",
            f"Domain: {{self.domain}}",
            f"Task: {{context.input_text}}",
            "",
            "Execution plan:",
            *[f"- {{item}}" for item in context.plan],
            "",
            "Skill routing:",
            *[f"- {{item}}" for item in context.skill_notes],
        ]
        if context.connector_results:
            answer_lines.extend(["", "Connector status:", *[f"- {{item}}" for item in context.connector_results]])
        if context.knowledge:
            answer_lines.extend(["", "Knowledge context:", *[f"- {{item}}" for item in context.knowledge]])

        return AgentResult(
            answer="\\n".join(answer_lines),
            next_steps=[
                "Configure real knowledge bases, skills, and connectors in manifest.json.",
                "Replace placeholder adapters with production implementations.",
                "Run tests.json before publishing.",
            ],
            risks=[
                "Do not use unapproved data sources or secrets in the sandbox.",
                "Human review is required for sensitive customer, legal, finance, or permission changes.",
            ],
            sources=context.knowledge,
        )
'''


def generated_main(agent_name: str, prompt: str, domain: str, skills: list[str]) -> str:
    return DEFAULT_MAIN

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


def scaffold_manifest(agent_name: str, prompt: str, domain: str, skills: list[str], connectors: list[str], files: list[str] | None = None) -> dict:
    return {
        "name": agent_name,
        "description": zh("\\u9762\\u5411") + domain + zh("\\u7684 Atlas Agent \\u5e94\\u7528\\u8349\\u7a3f"),
        "framework": "atlas-agent-python",
        "entry": "main.py",
        "runtime": "python",
        "model": "qwen-turbo",
        "prompt": prompt,
        "agent": {"module": "agent.py", "class": "AtlasAgent"},
        "pipeline": ["plan", "retrieve", "use_skills", "use_connectors", "compose", "guard"],
        "io_schema": {
            "input": {"type": "string", "name": "input_text"},
            "output": {"type": "string", "format": "markdown"},
        },
        "knowledge_bases": [],
        "skills": skills,
        "connectors": connectors,
        "files": files or FRAMEWORK_FILES,
        "permissions": {"network": bool(connectors), "secrets": [], "filesystem": "sandbox"},
    }


def generated_tests(agent_name: str, sample_input: str) -> str:
    test_name = zh("\\u57fa\\u7840\\u8fd0\\u884c\\u9a8c\\u8bc1")
    return json.dumps(
        [{"name": test_name, "input": sample_input, "expected": agent_name}],
        ensure_ascii=False,
        indent=2,
    )


def generated_readme(agent_name: str, domain: str) -> str:
    return f'''# {agent_name}

This is an Atlas Agent project scaffold for: {domain}

## Runtime Flow

1. `main.py` exposes `main(input_text)` for the sandbox runner.
2. `agent.py` owns the `AtlasAgent` lifecycle: plan, retrieve, skill routing, connector calls, compose, and guard.
3. `runtime.py` defines typed context and result objects shared by the framework.
4. `skills.py`, `knowledge.py`, and `connectors.py` are adapters. Replace placeholders with approved implementations.
5. `tests.json` is used by the Atlas test page before publishing.

## Local Run

```bash
python main.py
```

Keep production secrets out of source files. Declare required permissions in `manifest.json`.
'''


def generated_preview(agent_name: str, domain: str) -> str:
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{agent_name}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; color: #172033; background: #f5f7fa; font-family: "Microsoft YaHei", sans-serif; }}
    main {{ min-height: 100vh; display: grid; grid-template-rows: auto 1fr auto; }}
    header {{ padding: 22px 28px; background: #172033; color: white; }}
    header small {{ color: #9fb0c8; }}
    section {{ padding: 28px; }}
    .message {{ max-width: 680px; padding: 18px; border: 1px solid #dbe2ea; background: white; border-radius: 6px; box-shadow: 0 8px 24px rgba(23,32,51,.08); }}
    footer {{ display: flex; gap: 10px; padding: 18px 28px; border-top: 1px solid #dbe2ea; background: white; }}
    input {{ flex: 1; padding: 12px; border: 1px solid #c9d3df; border-radius: 4px; }}
    button {{ padding: 0 18px; border: 0; border-radius: 4px; color: white; background: #2463eb; font-weight: 700; }}
  </style>
</head>
<body>
  <main>
    <header><h1>{agent_name}</h1><small>{domain}</small></header>
    <section><div class="message">这是 Agent 的前端预览。真实对话和运行结果由 Atlas 右侧运行面板提供。</div></section>
    <footer><input aria-label="任务输入" placeholder="输入任务..." /><button type="button">发送</button></footer>
  </main>
</body>
</html>'''


def secure_preview_html(html: str) -> str:
    policy = (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "img-src data: blob:; font-src data:; connect-src 'none'; form-action 'none'; "
        "base-uri 'none'; frame-src 'none'; object-src 'none'"
    )
    meta = f'<meta http-equiv="Content-Security-Policy" content="{policy}">'
    if re.search(r"<head[^>]*>", html, flags=re.I):
        return re.sub(r"(<head[^>]*>)", lambda match: match.group(1) + meta, html, count=1, flags=re.I)
    return meta + html


def scaffold_agent_files(agent_name: str, prompt: str, domain: str, skills: list[str], connectors: list[str], sample_input: str, needs_preview: bool = False) -> dict[str, str]:
    file_names = [*FRAMEWORK_FILES, *(["preview.html"] if needs_preview else [])]
    files = {
        "main.py": generated_main(agent_name, prompt, domain, skills),
        "agent.py": generated_agent(agent_name, prompt, domain, skills, connectors),
        "runtime.py": generated_runtime(),
        "skills.py": generated_skills_module(skills),
        "knowledge.py": generated_knowledge_module(),
        "connectors.py": generated_connectors_module(connectors),
        "SKILL.md": generated_skill(agent_name, domain, skills),
        "tests.json": generated_tests(agent_name, sample_input),
        "README.md": generated_readme(agent_name, domain),
    }
    if needs_preview:
        files["preview.html"] = generated_preview(agent_name, domain)
    files["manifest.json"] = json.dumps(
        scaffold_manifest(agent_name, prompt, domain, skills, connectors, file_names),
        ensure_ascii=False,
        indent=2,
    )
    return files


@router.post("/generate")
def generate_agent_app(payload: GenerateAgentRequest, user: User = Depends(current_user), ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    blueprint = infer_agent_blueprint(payload.message, payload.project_name)
    agent = Agent(name=blueprint["name"], description=blueprint["domain"], kind="code",
                  model="qwen-turbo", workspace_id=ws, status="draft", created_by=user.id)
    db.add(agent)
    db.flush()  # 拿到 agent.id，用作工作区目录名 = draft_id
    draft_dir = DRAFT_ROOT / agent.id
    draft_dir.mkdir(parents=True, exist_ok=True)
    files = scaffold_agent_files(
        agent_name=blueprint["name"],
        prompt=blueprint["prompt"],
        domain=blueprint["domain"],
        skills=blueprint["skills"],
        connectors=blueprint["connectors"],
        sample_input=blueprint["sample_input"],
        needs_preview=blueprint["needs_preview"],
    )
    for path, content in files.items():
        (draft_dir / path).write_text(content, encoding="utf-8")
    create_version(db, agent, snapshot_code_agent(draft_dir), label="draft", note="")
    db.commit()
    reply = (
        zh("\\u5df2\\u751f\\u6210 Atlas Agent \\u9879\\u76ee\\u8349\\u7a3f\\uff1a") + blueprint["name"] + "\n\n"
        + f"已创建 {len(files)} 个 Agent 项目文件：" + "、".join(files.keys()) + "\n"
        + zh("\\u5e94\\u7528\\u573a\\u666f\\uff1a") + blueprint["domain"] + "\n"
        + zh("\\u4e0b\\u4e00\\u6b65\\uff1a\\u5728\\u5de6\\u4fa7\\u914d\\u7f6e Skills\\u3001\\u77e5\\u8bc6\\u5e93\\u548c\\u8fde\\u63a5\\u5668\\uff0c\\u518d\\u8fdb\\u5165\\u6d4b\\u8bd5\\u3001\\u53d1\\u5e03\\u68c0\\u67e5\\u548c\\u53d1\\u5e03\\u9875\\u9762\\u8dd1\\u5b8c\\u95ed\\u73af\\u3002")
    )
    append_builder_message(draft_dir, "user", payload.message)
    append_builder_message(draft_dir, "assistant", reply, changed_files=list(files.keys()), version_no=1)
    return {"draft": {"id": agent.id, "name": blueprint["name"], "status": "draft"}, "reply": reply, "blueprint": blueprint, "files": list(files.keys())}


def parse_llm_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I | re.S)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def fallback_refinement(files: dict[str, str], message: str) -> tuple[dict[str, str], list[str]]:
    changed: list[str] = []
    manifest = json.loads(files.get("manifest.json", "{}"))
    prompt = str(manifest.get("prompt") or "")
    if message not in prompt:
        manifest["prompt"] = prompt.rstrip() + f"\n\n补充产品要求：{message.strip()}"
    manifest["description"] = str(manifest.get("description") or "Agent 应用")
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2)
    changed.append("manifest.json")

    agent_code = files.get("agent.py", "")
    if agent_code and "system_prompt =" in agent_code:
        files["agent.py"] = re.sub(
            r"^\s*system_prompt\s*=.*$",
            lambda _match: f"    system_prompt = {manifest['prompt']!r}",
            agent_code,
            count=1,
            flags=re.M,
        )
        changed.append("agent.py")

    requirements = files.get("requirements.md", "# Product change log\n")
    requirements += f"\n- {datetime.now(timezone.utc).isoformat()}: {message.strip()}\n"
    files["requirements.md"] = requirements
    changed.append("requirements.md")
    return files, changed


@router.post("/drafts/{draft_id}/refine")
async def refine_agent_app(draft_id: str, payload: RefineAgentRequest, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="请输入要修改的需求")
    agent = agent_or_404(draft_id, ws, db)
    if agent.kind != "code":
        raise HTTPException(status_code=400, detail="只有代码型 Agent 支持会话式修改")
    draft_dir = draft_dir_or_404(draft_id, ws, db)
    before_files = read_workspace_files(draft_dir)
    files = dict(before_files)
    append_builder_message(draft_dir, "user", message)

    manifest = read_manifest(draft_dir)
    context_files = {
        path: content[:16000]
        for path, content in files.items()
        if path in {"manifest.json", "agent.py", "main.py", "skills.py", "knowledge.py", "connectors.py", "preview.html", "README.md"}
    }
    instruction = (
        "你是 Atlas Coding Agent。根据用户要求修改现有 Agent 项目。"
        "只返回 JSON：{\"summary\":\"...\",\"files\":{\"path\":\"完整文件内容\"}}。"
        "files 只包含需要新增或修改的文本文件；必须保留 main(input_text) 入口与合法 manifest.json。"
        "不要返回解释、Markdown 代码围栏或删除指令。"
    )
    llm_text = ""
    try:
        llm_text = await complete(
            [
                {"role": "system", "content": instruction},
                {"role": "user", "content": "当前项目：\n" + json.dumps(context_files, ensure_ascii=False) + "\n\n修改要求：" + message},
            ],
            str(manifest.get("model") or agent.model),
            temperature=0.1,
            max_tokens=5000,
        )
    except Exception:
        llm_text = ""

    parsed = parse_llm_json(llm_text) if llm_text else {}
    proposed = parsed.get("files") if isinstance(parsed.get("files"), dict) else {}
    changed_files: list[str] = []
    allowed_suffixes = {".py", ".json", ".md", ".html", ".css", ".js", ".txt", ".yaml", ".yml"}
    for path, content in proposed.items():
        if not isinstance(path, str) or not isinstance(content, str) or len(content) > 200_000:
            continue
        if Path(path).suffix.lower() not in allowed_suffixes:
            continue
        safe_path(draft_dir, path)
        files[path] = content
        changed_files.append(path)

    used_fallback = not changed_files
    if used_fallback:
        files, changed_files = fallback_refinement(files, message)

    try:
        manifest_next = json.loads(files.get("manifest.json", "{}"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Coding Agent 生成的 manifest 无效：{exc}") from exc
    manifest_next["files"] = sorted(files.keys())
    files["manifest.json"] = json.dumps(manifest_next, ensure_ascii=False, indent=2)
    if "manifest.json" not in changed_files:
        changed_files.append("manifest.json")

    write_workspace_files(draft_dir, files)
    _, errors, warnings = validate_manifest(draft_dir)
    if errors:
        write_workspace_files(draft_dir, before_files)
        raise HTTPException(status_code=400, detail="修改未应用：" + "；".join(errors))

    version = create_version(db, agent, snapshot_code_agent(draft_dir), label="history", note=message[:120])
    agent.name = str(manifest_next.get("name") or agent.name)[:120]
    agent.description = str(manifest_next.get("description") or agent.description)[:500]
    agent.model = str(manifest_next.get("model") or agent.model)[:120]
    db.commit()
    summary = str(parsed.get("summary") or "已根据新要求更新 Agent 的提示词、代码约束和项目变更记录。")
    if used_fallback:
        summary += " 当前模型不可用，因此使用了可审计的本地修改方案；配置模型 Key 后可进行更细粒度代码改写。"
    reply = f"{summary}\n\n已生成 v{version.version_no}，修改文件：{'、'.join(changed_files)}"
    append_builder_message(
        draft_dir,
        "assistant",
        reply,
        changed_files=changed_files,
        version_no=version.version_no,
        warnings=warnings,
    )
    return {
        "reply": reply,
        "changed_files": changed_files,
        "version_no": version.version_no,
        "warnings": warnings,
        "draft": {"id": agent.id, "name": agent.name, "status": agent.status},
    }


def eval_run_payload(run: AgentEvalRun | None) -> dict | None:
    if not run:
        return None
    try:
        results = json.loads(run.results_json)
    except json.JSONDecodeError:
        results = []
    avg_elapsed = sum(float(item.get("elapsed_ms") or 0) for item in results) / len(results) if results else 0.0
    return {
        "id": run.id,
        "kind": run.kind,
        "ok": run.ok,
        "passed": run.passed,
        "total": run.total,
        "pass_rate": run.pass_rate,
        "avg_elapsed_ms": round(avg_elapsed, 1),
        "results": results,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


@router.get("/drafts/{draft_id}/product-state")
def get_draft_product_state(draft_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    agent = agent_or_404(draft_id, ws, db)
    draft_dir = draft_dir_or_404(draft_id, ws, db) if agent.kind == "code" else None
    current = db.get(AgentVersion, agent.current_version_id) if agent.current_version_id else None
    dirty = False
    if draft_dir and current:
        dirty = version_snapshot(current).get("files", {}) != read_workspace_files(draft_dir)
    latest_evaluation = eval_run_payload(latest_eval_run(db, agent.id, "evaluate"))
    if latest_evaluation:
        manifest = read_manifest(draft_dir) if draft_dir else {}
        latest_evaluation["summary"] = build_evaluation_summary(
            latest_evaluation["pass_rate"],
            latest_evaluation["avg_elapsed_ms"],
            manifest.get("skills") if isinstance(manifest.get("skills"), list) else [],
            manifest.get("connectors") if isinstance(manifest.get("connectors"), list) else [],
            latest_evaluation["results"],
        )
    return {
        "agent": AgentOut.model_validate(agent).model_copy(update=agent_out_fields(agent, db)),
        "draft_dirty": dirty,
        "latest_test": eval_run_payload(latest_eval_run(db, agent.id, "test")),
        "latest_evaluation": latest_evaluation,
        "deploy_config": parse_deploy_config(agent.deploy_config_json),
        "publish_checklist": checklist_for_agent(agent, db),
        "builder_messages": read_builder_messages(draft_dir) if draft_dir else [],
        "preview_available": bool(draft_dir and (draft_dir / "preview.html").exists()),
    }


@router.get("/drafts/{draft_id}/preview")
def get_draft_preview(draft_id: str, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    draft_dir = draft_dir_or_404(draft_id, ws, db)
    preview = draft_dir / "preview.html"
    return {
        "exists": preview.exists(),
        "path": "preview.html" if preview.exists() else "",
        "html": secure_preview_html(preview.read_text(encoding="utf-8")) if preview.exists() else "",
    }


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
    agent = agent_or_404(draft_id, ws, db)
    agent.updated_at = datetime.now(timezone.utc)
    db.commit()

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
    agent = agent_or_404(draft_id, ws, db)
    agent.updated_at = datetime.now(timezone.utc)
    db.commit()

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
    agent = agent_or_404(draft_id, ws, db)
    agent.updated_at = datetime.now(timezone.utc)
    db.commit()

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
    agent = agent_or_404(draft_id, ws, db)
    agent.updated_at = datetime.now(timezone.utc)
    db.commit()

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
    agent = agent_or_404(draft_id, ws, db)
    draft_dir_or_404(draft_id, ws, db)
    input_text = payload.input_text if payload else "test input"
    result = await execute_agent_runtime(
        agent,
        input_text,
        db,
        use_published=False,
        source="preview",
        confirmed_tools=payload.confirmed_tools if payload else [],
    )
    record_eval_run(db, draft_id, "test", result.get("ok", False), 1 if result.get("ok") else 0, 1, [result])
    return result


@router.post("/drafts/{draft_id}/evaluate")
async def evaluate_draft_app(draft_id: str, payload: EvaluateDraftRequest, ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    draft_dir = draft_dir_or_404(draft_id, ws, db)
    agent = agent_or_404(draft_id, ws, db)
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
        run = await execute_agent_runtime(
            agent,
            item.input,
            db,
            use_published=False,
            source="evaluate",
            record_log_override=False,
        )
        output = run.get("answer", "")
        ok = bool(run.get("ok")) and (not item.expected or item.expected.lower() in output.lower())
        passed += 1 if ok else 0
        result = {
            "name": item.name,
            "input": item.input,
            "expected": item.expected,
            "ok": ok,
            "output": output,
            "logs": run.get("logs", ""),
            "elapsed_ms": run.get("elapsed_ms", 0),
            "sources": run.get("sources", []),
            "trace": run.get("trace", []),
            "tool_calls": run.get("tool_calls", []),
        }
        if not ok:
            reason = classify_failure(run.get("ok", False), run.get("error"), item.expected, output)
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
    summary = build_evaluation_summary(passed / len(cases), avg_elapsed_ms, declared_skills, declared_connectors, results)

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
    checklist = build_publish_checklist(
        kind=agent.kind,
        has_current_version=bool(agent.current_version_id),
        has_passed_test=bool(latest_test and latest_test.ok),
        last_eval_ok=(latest_eval.ok if latest_eval else None),
        deploy_config_configured=deploy_config_configured,
        allowed_connectors=deploy_config.get("allowed_connectors") or [],
        high_risk_approved=bool(deploy_config.get("high_risk_approved", False)),
    )
    release_suite = db.scalar(
        select(EvaluationSuite)
        .where(EvaluationSuite.agent_id == agent.id, EvaluationSuite.is_release_gate.is_(True))
        .order_by(EvaluationSuite.created_at.desc())
    )
    if release_suite:
        gate_run = db.scalar(
            select(AgentEvalRun)
            .where(
                AgentEvalRun.agent_id == agent.id,
                AgentEvalRun.kind == "suite",
                AgentEvalRun.suite_id == release_suite.id,
                AgentEvalRun.agent_version_id == agent.current_version_id,
            )
            .order_by(AgentEvalRun.created_at.desc())
        )
        checklist["items"].append({
            "key": "release_evaluation",
            "label": f"发布评测已通过（{release_suite.name}）",
            "ok": bool(gate_run and gate_run.ok),
            "level": "blocking",
        })
        checklist["can_publish"] = checklist["can_publish"] and bool(gate_run and gate_run.ok)
    return checklist


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
    if agent.kind == "code":
        draft_dir = draft_dir_or_404(agent_id, ws, db)
        live_snapshot = snapshot_code_agent(draft_dir)
        if version_snapshot(current) != live_snapshot:
            current = create_version(db, agent, live_snapshot, label="history", note="发布前自动保存当前工作区")
            db.flush()
    published_snapshot = dict(version_snapshot(current))
    published_snapshot["evaluation"] = eval_run_payload(latest_eval_run(db, agent.id, "evaluate"))
    published_snapshot["deploy_config"] = parse_deploy_config(agent.deploy_config_json)
    published = create_version(db, agent, published_snapshot, label="published", note=f"发布自 v{current.version_no}")
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
def update_deploy_config(agent_id: str, payload: DeployConfigUpdate, user: User = Depends(current_user), ws: str = Depends(current_workspace_id), db: Session = Depends(get_db)):
    agent = agent_or_404(agent_id, ws, db)
    if payload.high_risk_approved:
        membership = db.scalar(select(Membership).where(Membership.workspace_id == ws, Membership.user_id == user.id))
        if not membership or membership.role != "owner":
            raise HTTPException(status_code=403, detail="只有工作空间所有者可以批准高风险连接器")
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
        try:
            output_payload = json.loads(run.output_json) if run.output_json else {}
        except json.JSONDecodeError:
            output_payload = {}
        usage = output_payload.get("usage") if isinstance(output_payload, dict) else None
        entries.append(CallLogEntryOut(
            id=run.id, time=run.created_at, source=run.source, status=run.status,
            latency_ms=latency_ms, error=run.error,
            request_path=f"/api/agents/{agent_id}/invoke" if run.source == "api" else "工作台",
            status_code=200 if run.status == "succeeded" else (202 if run.status == "waiting_confirmation" else 500),
            token_usage=usage.get("total_tokens") if isinstance(usage, dict) else None,
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
    agent = agent_or_404(agent_id, ws, db)
    if agent.status != "published":
        raise HTTPException(status_code=400, detail="请先发布 Agent，再生成 API Key")
    if not parse_deploy_config(agent.deploy_config_json).get("api_access", False):
        raise HTTPException(status_code=400, detail="请先在落地配置中开启外部 API 调用")
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
