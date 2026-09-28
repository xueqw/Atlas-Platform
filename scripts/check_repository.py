"""Enforce repository presentation and credential hygiene policies."""

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
TEXT_SUFFIXES = {
    ".css", ".html", ".js", ".json", ".md", ".py", ".ps1", ".sh",
    ".svg", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml",
}


def tracked_files() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=ROOT, text=False
    )
    return [ROOT / item.decode("utf-8") for item in output.split(b"\0") if item]


def main() -> None:
    failures: list[str] = []
    for path in tracked_files():
        relative = path.relative_to(ROOT)
        if not path.exists():
            continue
        if path.name == ".env":
            failures.append(f"tracked environment file: {relative}")
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        if CJK.search(content):
            failures.append(f"non-English repository text: {relative}")

    if failures:
        raise SystemExit("Repository policy failed:\n- " + "\n- ".join(failures))
    print("Repository presentation and credential policies passed.")


if __name__ == "__main__":
    main()
