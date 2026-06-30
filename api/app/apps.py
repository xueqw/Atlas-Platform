from pathlib import Path
from uuid import uuid4
import subprocess
import sys

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


def safe_path(root: Path, relative_path: str) -> Path:
    target = (root / relative_path).resolve()
    root_resolved = root.resolve()

    if not str(target).startswith(str(root_resolved)):
        raise HTTPException(status_code=400, detail="非法文件路径")

    return target


def ensure_demo_files(draft_dir: Path, app_name: str) -> None:
    draft_dir.mkdir(parents=True, exist_ok=True)

    main_file = draft_dir / "main.py"
    manifest_file = draft_dir / "manifest.json"

    if not main_file.exists():
        main_file.write_text(
            '''def main(input_text: str):
    return "Hello Atlas: " + input_text


if __name__ == "__main__":
    print(main("test input"))
''',
            encoding="utf-8",
        )

    if not manifest_file.exists():
        manifest_file.write_text(
            f'''{{
  "name": "{app_name}",
  "description": "A demo Agent App",
  "entry": "main.py",
  "runtime": "python",
  "permissions": {{
    "network": false,
    "secrets": []
  }}
}}
''',
            encoding="utf-8",
        )


@router.post("/drafts")
def create_app_draft(payload: CreateDraftRequest):
    draft_id = f"draft_{uuid4().hex[:8]}"
    draft_dir = DRAFT_ROOT / draft_id

    ensure_demo_files(draft_dir, payload.name)

    return {
        "id": draft_id,
        "name": payload.name,
        "status": "draft",
    }


@router.get("/drafts/{draft_id}/files")
def list_draft_files(draft_id: str):
    draft_dir = DRAFT_ROOT / draft_id

    if not draft_dir.exists():
        raise HTTPException(status_code=404, detail="草稿不存在")

    files = []

    for item in sorted(draft_dir.rglob("*")):
        if item.is_dir():
            continue

        relative_path = item.relative_to(draft_dir).as_posix()

        files.append(
            {
                "path": relative_path,
                "name": item.name,
                "type": "file",
            }
        )

    return files


@router.get("/drafts/{draft_id}/files/content")
def get_draft_file(draft_id: str, path: str):
    draft_dir = DRAFT_ROOT / draft_id

    if not draft_dir.exists():
        raise HTTPException(status_code=404, detail="草稿不存在")

    file_path = safe_path(draft_dir, path)

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    return {
        "path": path,
        "content": file_path.read_text(encoding="utf-8"),
    }


@router.put("/drafts/{draft_id}/files/content")
def save_draft_file(draft_id: str, payload: SaveFileRequest):
    draft_dir = DRAFT_ROOT / draft_id

    if not draft_dir.exists():
        raise HTTPException(status_code=404, detail="草稿不存在")

    file_path = safe_path(draft_dir, payload.path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(payload.content, encoding="utf-8")

    return {"ok": True}


@router.post("/drafts/{draft_id}/run")
def run_draft_app(draft_id: str):
    draft_dir = DRAFT_ROOT / draft_id

    if not draft_dir.exists():
        raise HTTPException(status_code=404, detail="草稿不存在")

    main_file = draft_dir / "main.py"

    if not main_file.exists():
        raise HTTPException(status_code=400, detail="缺少入口文件 main.py")

    try:
        result = subprocess.run(
            [sys.executable, str(main_file)],
            cwd=str(draft_dir),
            capture_output=True,
            text=True,
            timeout=10,
        )

        logs = ""
        logs += f"> draft_id: {draft_id}\n"
        logs += "> 执行 main.py\n\n"

        if result.stdout:
            logs += result.stdout

        if result.stderr:
            logs += "\n[stderr]\n"
            logs += result.stderr

        logs += f"\n> exit code: {result.returncode}\n"

        return {
            "ok": result.returncode == 0,
            "logs": logs,
            "error": result.stderr if result.returncode != 0 else None,
        }

    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "logs": "> 运行超时：超过 10 秒",
            "error": "timeout",
        }
