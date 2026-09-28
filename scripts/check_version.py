"""Verify that every public Atlas version marker matches the canonical VERSION file."""

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


def package_version(path: Path) -> str:
    return json.loads(path.read_text(encoding="utf-8"))["version"]


def main() -> None:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not SEMVER.fullmatch(version):
        raise SystemExit(f"VERSION is not valid Semantic Versioning: {version!r}")

    versions = {
        "package.json": package_version(ROOT / "package.json"),
        "web/package.json": package_version(ROOT / "web/package.json"),
        "web/package-lock.json": package_version(ROOT / "web/package-lock.json"),
    }
    mismatches = {name: value for name, value in versions.items() if value != version}
    if mismatches:
        details = ", ".join(f"{name}={value}" for name, value in mismatches.items())
        raise SystemExit(f"Version mismatch: VERSION={version}, {details}")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if f"Current version: **v{version}**" not in readme:
        raise SystemExit(f"README version marker must be: Current version: **v{version}**")

    print(f"Version {version} is consistent across Atlas.")


if __name__ == "__main__":
    main()
