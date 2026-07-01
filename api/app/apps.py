from pathlib import Path
from uuid import uuid4
import json
import re
import subprocess
import sys
import time
import httpx

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .model_gateway import resolve_provider


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


def draft_dir_or_404(draft_id: str) -> Path:
    draft_dir = DRAFT_ROOT / draft_id

    if not draft_dir.exists():
        raise HTTPException(status_code=404, detail="草稿不存在")

    return draft_dir


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
            "error": None,
            "warnings": warnings,
            "elapsed_ms": elapsed_ms,
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
        "error": result.stderr if result.returncode != 0 else None,
        "warnings": warnings,
        "elapsed_ms": elapsed_ms,
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
def generate_agent_app(payload: GenerateAgentRequest):
    blueprint = infer_agent_blueprint(payload.message, payload.project_name)
    draft_id = f"draft_{uuid4().hex[:8]}"
    draft_dir = DRAFT_ROOT / draft_id
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
    reply = (
        zh("\\u5df2\\u751f\\u6210 Atlas Agent \\u9879\\u76ee\\u8349\\u7a3f\\uff1a") + blueprint["name"] + "\n\n"
        + zh("\\u5df2\\u521b\\u5efa 4 \\u4e2a\\u53ef\\u8fd0\\u884c\\u6587\\u4ef6\\uff1amanifest.json\\u3001main.py\\u3001SKILL.md\\u3001tests.json") + "\n"
        + zh("\\u5e94\\u7528\\u573a\\u666f\\uff1a") + blueprint["domain"] + "\n"
        + zh("\\u4e0b\\u4e00\\u6b65\\uff1a\\u8fdb\\u5165 Web IDE \\u8c03\\u6574\\u4ee3\\u7801\\u3001\\u63a5\\u5165\\u77e5\\u8bc6\\u5e93\\u6216\\u8fd0\\u884c\\u6d4b\\u8bd5\\uff0c\\u7136\\u540e\\u53ef\\u4f5c\\u4e3a\\u5e94\\u7528\\u6295\\u653e\\u539f\\u578b\\u7ee7\\u7eed\\u5b8c\\u5584\\u3002")
    )
    return {"draft": {"id": draft_id, "name": blueprint["name"], "status": "draft"}, "reply": reply, "blueprint": blueprint, "files": ["manifest.json", "main.py", "SKILL.md", "tests.json"]}


@router.post("/drafts")
def create_app_draft(payload: CreateDraftRequest):
    draft_id = f"draft_{uuid4().hex[:8]}"
    draft_dir = DRAFT_ROOT / draft_id

    ensure_demo_files(draft_dir, payload.name.strip() or "demo-agent-app")

    return {
        "id": draft_id,
        "name": payload.name.strip() or "demo-agent-app",
        "status": "draft",
    }


@router.get("/drafts/{draft_id}/files")
def list_draft_files(draft_id: str):
    draft_dir = draft_dir_or_404(draft_id)
    files = []

    for item in sorted(draft_dir.rglob("*")):
        if item.is_dir() or item.name.startswith(".atlas_"):
            continue

        relative_path = item.relative_to(draft_dir).as_posix()
        files.append({"path": relative_path, "name": item.name, "type": "file"})

    return files


@router.post("/drafts/{draft_id}/files")
def create_draft_file(draft_id: str, payload: CreateFileRequest):
    draft_dir = draft_dir_or_404(draft_id)
    file_path = safe_path(draft_dir, payload.path)

    if file_path.exists():
        raise HTTPException(status_code=409, detail="文件已存在")

    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(payload.content, encoding="utf-8")

    return {"ok": True, "path": file_path.relative_to(draft_dir).as_posix()}


@router.get("/drafts/{draft_id}/files/content")
def get_draft_file(draft_id: str, path: str):
    draft_dir = draft_dir_or_404(draft_id)
    file_path = safe_path(draft_dir, path)

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    return {"path": path, "content": file_path.read_text(encoding="utf-8")}


@router.put("/drafts/{draft_id}/files/content")
def save_draft_file(draft_id: str, payload: SaveFileRequest):
    draft_dir = draft_dir_or_404(draft_id)
    file_path = safe_path(draft_dir, payload.path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(payload.content, encoding="utf-8")

    return {"ok": True}


@router.put("/drafts/{draft_id}/files/rename")
def rename_draft_file(draft_id: str, payload: RenameFileRequest):
    draft_dir = draft_dir_or_404(draft_id)
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
def delete_draft_file(draft_id: str, path: str):
    draft_dir = draft_dir_or_404(draft_id)
    file_path = safe_path(draft_dir, path)

    if file_path.name in {"main.py", "manifest.json"}:
        raise HTTPException(status_code=400, detail="核心文件不能删除")

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    file_path.unlink()

    return {"ok": True}


@router.get("/drafts/{draft_id}/manifest/validate")
def validate_draft_manifest(draft_id: str):
    draft_dir = draft_dir_or_404(draft_id)
    manifest, errors, warnings = validate_manifest(draft_dir)

    return {
        "ok": not errors,
        "manifest": manifest,
        "errors": errors,
        "warnings": warnings,
    }


@router.post("/drafts/{draft_id}/run")
async def run_draft_app(draft_id: str, payload: RunDraftRequest | None = None):
    draft_dir = draft_dir_or_404(draft_id)
    input_text = payload.input_text if payload else "test input"

    model_result = await run_model_entry(draft_dir, input_text)
    if model_result and model_result.get("ok"):
        return model_result

    fallback = run_python_entry(draft_dir, input_text)
    if model_result and model_result.get("error"):
        fallback["warnings"] = [*(fallback.get("warnings") or []), *(model_result.get("warnings") or [])]
        f"已启用能力：{', '.join(str(item) for item in skills) or '基础任务处理'}",
    return fallback


@router.post("/drafts/{draft_id}/evaluate")
def evaluate_draft_app(draft_id: str, payload: EvaluateDraftRequest):
    draft_dir = draft_dir_or_404(draft_id)
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
        results.append(
            {
                "name": item.name,
                "input": item.input,
                "expected": item.expected,
                "ok": ok,
                "output": output,
                "logs": run.get("logs", ""),
                "elapsed_ms": run.get("elapsed_ms", 0),
            }
        )

    return {
        "ok": passed == len(cases),
        "passed": passed,
        "total": len(cases),
        "pass_rate": round(passed / len(cases), 4),
        "results": results,
    }
