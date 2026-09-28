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
            "description": "AI agent app draft",
            "entry": "main.py",
            "runtime": "python",
            "model": "gpt-4.1-mini",
            "prompt": "You are a reliable business AI agent. Give clear, actionable answers grounded in the available context.",
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
        raise HTTPException(status_code=400, detail="Invalid file path")

    target = (root / normalized).resolve()
    root_resolved = root.resolve()

    if root_resolved != target and root_resolved not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid file path")

    return target


def draft_dir_or_404(draft_id: str) -> Path:
    draft_dir = DRAFT_ROOT / draft_id

    if not draft_dir.exists():
        raise HTTPException(status_code=404, detail="Draft not found")

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
                        "name": "Basic greeting",
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
        raise HTTPException(status_code=400, detail="manifest.json is missing")

    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid manifest JSON at line {exc.lineno}") from exc

    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="The manifest must be a JSON object")

    return data


def validate_manifest(draft_dir: Path) -> tuple[dict, list[str], list[str]]:
    data = read_manifest(draft_dir)
    errors: list[str] = []
    warnings: list[str] = []

    entry = data.get("entry", "main.py")

    if not data.get("name"):
        errors.append("The app name is required")

    if data.get("runtime") != "python":
        errors.append("The current sandbox supports only the Python runtime")

    if not isinstance(entry, str) or not entry.endswith(".py"):
        errors.append("entry must point to a Python file")
    elif not safe_path(draft_dir, entry).exists():
        errors.append(f"Entry file not found: {entry}")

    permissions = data.get("permissions") or {}

    if permissions.get("network"):
        warnings.append("The preview sandbox blocks outbound network access; production access requires an allowlist")

    if permissions.get("secrets"):
        warnings.append("Secrets are declared; verify their scope before publishing")

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
        f"You are the deployed agent named {name}.",
        f"App description: {description}",
        "Enabled capabilities: " + (", ".join(str(item) for item in skills) or "general task support"),
        "Answer the user's question directly. Do not describe yourself as a draft.",
        "Use a structured, actionable response. If live data or an external system is required and no tool result is available, explain the limitation and identify the missing input.",
        "For financial, medical, or legal topics, state the relevant risks and do not make final decisions for the user.",
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
                "> Model request failed; the local sandbox fallback remains available",
                f"> ERROR: {error}",
            ]),
            "error": str(error),
            "warnings": [*warnings, "Model request failed. Check the API key, base URL, model name, and network."],
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }


def run_python_entry(draft_dir: Path, input_text: str, timeout: int = 10) -> dict:
    manifest, errors, warnings = validate_manifest(draft_dir)

    if errors:
        return {
            "ok": False,
            "logs": "\n".join(["> Manifest validation failed", *[f"> ERROR: {item}" for item in errors]]),
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
        "    raise RuntimeError('The entry file must expose main(input_text)')\n"
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
            "logs": "> Sandbox timed out after 10 seconds",
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
        return "Custom Agent"
    if value.isascii():
        value = value[:1].upper() + value[1:]
    suffixes = ["Agent", zh("\u52a9\u624b"), zh("\u5de5\u5177"), zh("\u673a\u5668\u4eba"), zh("\u5e94\u7528"), zh("\u4e13\u5bb6"), zh("\u5206\u6790\u5e08"), zh("\u751f\u6210\u5668")]
    if not any(value.endswith(suffix) for suffix in suffixes):
        value += " Agent" if value.isascii() else zh("\u52a9\u624b")
    return value[:28]


def extract_agent_name(message: str) -> str:
    english = re.search(
        r"(?:build|create|make|generate)\s+(?:me\s+)?(?:an?\s+)?(.+?)(?:\s+(?:that|to|which|for)\b|[,.;]|$)",
        message,
        flags=re.IGNORECASE,
    )
    if english:
        return clean_agent_name(english.group(1))
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
    return "Custom Agent"

def slug_skill(value: str) -> str:
    mapping = {
        "customer support": "customer-service",
        "support": "customer-service",
        "ticket": "ticket-summary",
        "knowledge base": "knowledge-base-qa",
        "sales": "sales-assistant",
        "lead": "lead-qualification",
        "prospect": "lead-qualification",
        "account": "customer-summary",
        "question answering": "question-answering",
        "document": "document-search",
        "policy": "policy-search",
        "workflow": "workflow-guidance",
        "approval": "approval-checklist",
        "expense": "expense-guidance",
        "onboarding": "onboarding-guidance",
        "recruiting": "recruiting-assistant",
        "resume": "resume-screening",
        "interview": "interview-questioning",
        "candidate": "candidate-summary",
        "presentation": "presentation-outline",
        "slides": "presentation-outline",
        "pitch deck": "presentation-outline",
        "powerpoint": "presentation-outline",
        "spreadsheet": "spreadsheet-analysis",
        "excel": "spreadsheet-analysis",
        "data analysis": "data-analysis",
        "chart": "chart-generation",
        "github": "github-workflow",
        "repository": "repository-analysis",
        "pull request": "pull-request-review",
        "issue": "issue-triage",
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
    text = message.strip() or "Build a business task agent"
    lowered = text.lower()
    old_default = {"Atlas Business Agent Project", zh("Atlas \u4f01\u4e1a\u52a9\u624b\u9879\u76ee"), zh("\u6263\u5b50\u7684\u65b0\u9879\u76ee"), "demo-agent-app"}
    name = project_name.strip() if project_name.strip() and project_name.strip() not in old_default else extract_agent_name(text)

    keyword_groups = [
        ("customer support", "Customer support and ticket resolution", ["customer support", "support", "ticket", "knowledge base"], []),
        ("sales", "Sales lead qualification and account follow-up", ["sales", "lead", "prospect", "account"], []),
        ("knowledge base", "Company knowledge base question answering", ["knowledge base", "question answering", "document", "policy"], []),
        ("workflow", "Internal workflow and policy guidance", ["workflow", "approval", "expense", "onboarding"], []),
        ("recruiting", "Recruiting, resume screening, and interview support", ["recruiting", "resume", "interview", "candidate"], []),
        ("presentation", "Presentation outlines and slide content", ["presentation", "slides", "pitch deck", "powerpoint"], []),
        ("spreadsheet", "Spreadsheet processing and data analysis", ["spreadsheet", "excel", "data analysis", "chart"], []),
        ("software", "GitHub repository and engineering workflow support", ["github", "repository", "pull request", "issue"], ["github"]),
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
        words = re.findall(r"[\u4e00-\u9fa5A-Za-z0-9][\u4e00-\u9fa5A-Za-z0-9 -]{1,18}", text)[:4] or ["task", "planning"]
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

    prompt = (
        f"You are {name}. Your job is to help users with {domain}. "
        "Understand the request before producing a structured result, recommended next steps, and relevant risks. "
        "When information is missing, state what you need instead of inventing facts."
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
    title = "Purpose"
    ability = "Capabilities"
    rules = "Operating rules"
    rule_1 = "- Keep responses concise, actionable, and appropriate for a business audience."
    rule_2 = "- Call out missing information instead of inventing evidence."
    rule_3 = "- Recommend human review for sensitive customer, contract, or permission decisions."
    rule_4 = "- Prefer approved knowledge bases and tool results when available."
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
        "description": f"Atlas agent draft for {blueprint['domain']}",
        "entry": "main.py",
        "runtime": "python",
        "model": "gpt-4.1-mini",
        "prompt": blueprint["prompt"],
        "knowledge_bases": [],
        "skills": blueprint["skills"],
        "connectors": blueprint["connectors"],
        "permissions": {"network": bool(blueprint["connectors"]), "secrets": [], "filesystem": "sandbox"},
    }
    (draft_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (draft_dir / "main.py").write_text(generated_main(blueprint["name"], blueprint["prompt"], blueprint["domain"], blueprint["skills"]), encoding="utf-8")
    (draft_dir / "SKILL.md").write_text(generated_skill(blueprint["name"], blueprint["domain"], blueprint["skills"]), encoding="utf-8")
    test_name = "Basic runtime check"
    (draft_dir / "tests.json").write_text(json.dumps([{"name": test_name, "input": blueprint["sample_input"], "expected": blueprint["name"]}], ensure_ascii=False, indent=2), encoding="utf-8")
    reply = (
        f"Created an Atlas agent draft: {blueprint['name']}\n\n"
        "Generated four runnable files: manifest.json, main.py, SKILL.md, and tests.json.\n"
        f"Use case: {blueprint['domain']}\n"
        "Next: open the Web IDE to review the code, connect a knowledge base, and run the evaluation suite."
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
        raise HTTPException(status_code=409, detail="File already exists")

    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(payload.content, encoding="utf-8")

    return {"ok": True, "path": file_path.relative_to(draft_dir).as_posix()}


@router.get("/drafts/{draft_id}/files/content")
def get_draft_file(draft_id: str, path: str):
    draft_dir = draft_dir_or_404(draft_id)
    file_path = safe_path(draft_dir, path)

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

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
        raise HTTPException(status_code=404, detail="Source file not found")

    if new_path.exists():
        raise HTTPException(status_code=409, detail="Destination file already exists")

    new_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.rename(new_path)

    return {"ok": True, "path": new_path.relative_to(draft_dir).as_posix()}


@router.delete("/drafts/{draft_id}/files")
def delete_draft_file(draft_id: str, path: str):
    draft_dir = draft_dir_or_404(draft_id)
    file_path = safe_path(draft_dir, path)

    if file_path.name in {"main.py", "manifest.json"}:
        raise HTTPException(status_code=400, detail="Core files cannot be deleted")

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

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
                raise HTTPException(status_code=400, detail=f"Invalid tests.json: {exc}") from exc

    if not cases:
        raise HTTPException(status_code=400, detail="Provide at least one evaluation case")

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
