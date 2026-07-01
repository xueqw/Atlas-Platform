from pathlib import Path
from uuid import uuid4
import json
import subprocess
import sys
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


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
    return value.encode("ascii").decode("unicode_escape")


def infer_agent_blueprint(message: str, project_name: str = "") -> dict:
    text = message.strip()
    lowered = text.lower()
    customer_words = [zh("\\u5ba2\\u670d"), zh("\\u552e\\u540e"), zh("\\u5de5\\u5355")]
    process_words = [zh("\\u6d41\\u7a0b"), zh("\\u5ba1\\u6279"), zh("\\u5236\\u5ea6"), zh("\\u62a5\\u9500"), zh("\\u5165\\u804c")]
    sales_words = [zh("\\u9500\\u552e"), zh("\\u7ebf\\u7d22"), zh("\\u5ba2\\u6237")]
    knowledge_words = [zh("\\u77e5\\u8bc6\\u5e93"), zh("\\u95ee\\u7b54"), zh("\\u6587\\u6863"), zh("\\u5236\\u5ea6")]

    if project_name.strip() and project_name.strip() != zh("\\u6263\\u5b50\\u7684\\u65b0\\u9879\\u76ee"):
        name = project_name.strip()
    elif any(word in text for word in customer_words):
        name = zh("\\u4f01\\u4e1a\\u5ba2\\u670d\\u52a9\\u624b")
    elif any(word in text for word in process_words):
        name = zh("\\u6d41\\u7a0b\\u529e\\u7406\\u52a9\\u624b")
    elif any(word in text for word in sales_words):
        name = zh("\\u9500\\u552e\\u7ebf\\u7d22\\u52a9\\u624b")
    elif any(word in text for word in knowledge_words):
        name = zh("\\u77e5\\u8bc6\\u5e93\\u95ee\\u7b54\\u52a9\\u624b")
    else:
        name = zh("\\u4f01\\u4e1a\\u4efb\\u52a1\\u52a9\\u624b")

    if any(word in text for word in customer_words):
        domain = zh("\\u5ba2\\u6237\\u670d\\u52a1\\u4e0e\\u552e\\u540e\\u5de5\\u5355")
        skills = ["faq-answering", "ticket-summary", "handoff-to-human"]
        connectors = ["feishu"]
        prompt = zh("\\u4f60\\u662f\\u4f01\\u4e1a\\u5ba2\\u670d\\u52a9\\u624b\\u3002\\u4f60\\u9700\\u8981\\u4f18\\u5148\\u4f9d\\u636e\\u77e5\\u8bc6\\u5e93\\u56de\\u7b54\\uff0c\\u7ed9\\u51fa\\u5904\\u7406\\u6b65\\u9aa4\\uff1b\\u5f53\\u4fe1\\u606f\\u4e0d\\u8db3\\u6216\\u6d89\\u53ca\\u6295\\u8bc9\\u5347\\u7ea7\\u65f6\\uff0c\\u660e\\u786e\\u5efa\\u8bae\\u8f6c\\u4eba\\u5de5\\u5904\\u7406\\u3002")
        sample_input = zh("\\u5ba2\\u6237\\u53cd\\u9988\\u8ba2\\u5355\\u5ef6\\u8fdf\\uff0c\\u8bf7\\u751f\\u6210\\u5904\\u7406\\u5efa\\u8bae\\u548c\\u56de\\u590d\\u8bdd\\u672f\\u3002")
    elif any(word in text for word in process_words):
        domain = zh("\\u5185\\u90e8\\u6d41\\u7a0b\\u4e0e\\u5236\\u5ea6\\u529e\\u7406")
        skills = ["policy-search", "workflow-checklist", "form-guidance"]
        connectors = ["feishu"]
        prompt = zh("\\u4f60\\u662f\\u4f01\\u4e1a\\u6d41\\u7a0b\\u529e\\u7406\\u52a9\\u624b\\u3002\\u4f60\\u9700\\u8981\\u6839\\u636e\\u5236\\u5ea6\\u548c\\u6d41\\u7a0b\\u8bf4\\u660e\\uff0c\\u5e2e\\u52a9\\u7528\\u6237\\u5224\\u65ad\\u529e\\u7406\\u8def\\u5f84\\u3001\\u6750\\u6599\\u6e05\\u5355\\u3001\\u5ba1\\u6279\\u8282\\u70b9\\u548c\\u6ce8\\u610f\\u4e8b\\u9879\\u3002")
        sample_input = zh("\\u6211\\u60f3\\u7533\\u8bf7\\u51fa\\u5dee\\u62a5\\u9500\\uff0c\\u9700\\u8981\\u51c6\\u5907\\u54ea\\u4e9b\\u6750\\u6599\\uff1f")
    elif any(word in text for word in sales_words):
        domain = zh("\\u9500\\u552e\\u7ebf\\u7d22\\u8ddf\\u8fdb\\u4e0e\\u5ba2\\u6237\\u6458\\u8981")
        skills = ["lead-qualification", "customer-summary", "follow-up-plan"]
        connectors = ["feishu"]
        prompt = zh("\\u4f60\\u662f\\u9500\\u552e\\u7ebf\\u7d22\\u52a9\\u624b\\u3002\\u4f60\\u9700\\u8981\\u603b\\u7ed3\\u5ba2\\u6237\\u80cc\\u666f\\u3001\\u8bc6\\u522b\\u8ddf\\u8fdb\\u4f18\\u5148\\u7ea7\\u3001\\u751f\\u6210\\u4e0b\\u4e00\\u6b65\\u884c\\u52a8\\u5efa\\u8bae\\uff0c\\u5e76\\u4fdd\\u6301\\u4fe1\\u606f\\u51c6\\u786e\\u53ef\\u8ffd\\u6eaf\\u3002")
        sample_input = zh("\\u5e2e\\u6211\\u6574\\u7406\\u8fd9\\u4e2a\\u5ba2\\u6237\\u7684\\u8ddf\\u8fdb\\u8ba1\\u5212\\u3002")
    elif any(word in text for word in knowledge_words):
        domain = zh("\\u4f01\\u4e1a\\u77e5\\u8bc6\\u5e93\\u95ee\\u7b54")
        skills = ["document-search", "answer-with-citation", "gap-detection"]
        connectors = []
        prompt = zh("\\u4f60\\u662f\\u4f01\\u4e1a\\u77e5\\u8bc6\\u5e93\\u95ee\\u7b54\\u52a9\\u624b\\u3002\\u4f60\\u5fc5\\u987b\\u4f18\\u5148\\u57fa\\u4e8e\\u77e5\\u8bc6\\u5e93\\u56de\\u7b54\\uff0c\\u7ed9\\u51fa\\u7b80\\u6d01\\u7ed3\\u8bba\\u548c\\u5f15\\u7528\\u4f9d\\u636e\\uff1b\\u5982\\u679c\\u8d44\\u6599\\u4e0d\\u8db3\\uff0c\\u9700\\u8981\\u8bf4\\u660e\\u7f3a\\u53e3\\u3002")
        sample_input = zh("\\u8bf7\\u6839\\u636e\\u77e5\\u8bc6\\u5e93\\u8bf4\\u660e\\u8fd9\\u9879\\u670d\\u52a1\\u7684\\u5f00\\u901a\\u6d41\\u7a0b\\u3002")
    else:
        domain = zh("\\u901a\\u7528\\u4f01\\u4e1a\\u4efb\\u52a1\\u6267\\u884c")
        skills = ["task-planning", "document-summary", "quality-check"]
        connectors = []
        prompt = zh("\\u4f60\\u662f\\u901a\\u7528\\u4f01\\u4e1a\\u4efb\\u52a1\\u52a9\\u624b\\u3002\\u4f60\\u9700\\u8981\\u5148\\u7406\\u89e3\\u7528\\u6237\\u76ee\\u6807\\uff0c\\u518d\\u62c6\\u89e3\\u6b65\\u9aa4\\uff0c\\u6700\\u540e\\u7ed9\\u51fa\\u6e05\\u6670\\u3001\\u53ef\\u6267\\u884c\\u3001\\u53ef\\u5ba1\\u8ba1\\u7684\\u7ed3\\u679c\\u3002")
        sample_input = text or zh("\\u5e2e\\u6211\\u5b8c\\u6210\\u4e00\\u4e2a\\u4f01\\u4e1a\\u5185\\u90e8\\u4efb\\u52a1\\u3002")

    if "agent" in lowered and name == zh("\\u4f01\\u4e1a\\u4efb\\u52a1\\u52a9\\u624b"):
        domain = zh("\\u81ea\\u5b9a\\u4e49 Agent \\u5e94\\u7528")
    return {"name": name, "domain": domain, "prompt": prompt, "skills": skills, "connectors": connectors, "sample_input": sample_input}


def generated_main(agent_name: str, prompt: str) -> str:
    received = zh("\\u5df2\\u6536\\u5230\\u4f60\\u7684\\u9700\\u6c42\\uff0c\\u6211\\u4f1a\\u6309\\u4f01\\u4e1a\\u53ef\\u6295\\u653e\\u6807\\u51c6\\u8f93\\u51fa\\u7ed3\\u679c\\u3002")
    task_label = zh("\\u4efb\\u52a1\\u7406\\u89e3\\uff1a")
    plan_title = zh("\\u6267\\u884c\\u8def\\u5f84\\uff1a")
    step_1 = zh("1. \\u5148\\u660e\\u786e\\u76ee\\u6807\\u3001\\u8fb9\\u754c\\u548c\\u9700\\u8981\\u7684\\u4fe1\\u606f\\u3002")
    step_2 = zh("2. \\u6309 Skill \\u80fd\\u529b\\u62c6\\u89e3\\u4efb\\u52a1\\uff0c\\u7ed9\\u51fa\\u53ef\\u64cd\\u4f5c\\u7684\\u56de\\u7b54\\u3002")
    step_3 = zh("3. \\u5bf9\\u4e0d\\u786e\\u5b9a\\u4fe1\\u606f\\u6807\\u6ce8\\u98ce\\u9669\\uff0c\\u5fc5\\u8981\\u65f6\\u5efa\\u8bae\\u4eba\\u5de5\\u590d\\u6838\\u3002")
    ending = zh("\\u8fd9\\u662f\\u521d\\u59cb\\u7248\\u8fd0\\u884c\\u7ed3\\u679c\\uff0c\\u53ef\\u5728 Web IDE \\u4e2d\\u7ee7\\u7eed\\u63a5\\u5165\\u77e5\\u8bc6\\u5e93\\u3001\\u5de5\\u5177\\u548c\\u6a21\\u578b\\u3002")
    demo_input = zh("\\u8bf7\\u5e2e\\u6211\\u5904\\u7406\\u4e00\\u4e2a\\u4f01\\u4e1a\\u4efb\\u52a1")
    return f'''SYSTEM_PROMPT = {prompt!r}


def main(input_text: str):
    lines = [
        {f"{agent_name} {received}"!r},
        "",
        {task_label!r} + input_text,
        "",
        {plan_title!r},
        {step_1!r},
        {step_2!r},
        {step_3!r},
        "",
        {ending!r},
    ]
    return "\\n".join(lines)


if __name__ == "__main__":
    print(main({demo_input!r}))
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
    (draft_dir / "main.py").write_text(generated_main(blueprint["name"], blueprint["prompt"]), encoding="utf-8")
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
def run_draft_app(draft_id: str, payload: RunDraftRequest | None = None):
    draft_dir = draft_dir_or_404(draft_id)
    input_text = payload.input_text if payload else "test input"

    return run_python_entry(draft_dir, input_text)


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
