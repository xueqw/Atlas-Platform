from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import time

from .config import settings


def run_python(project_dir: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    if settings.code_runner_backend != "docker":
        if settings.code_runner_required or settings.environment == "production":
            raise RuntimeError("Production code execution requires Docker runner")
        return subprocess.run(
            [shutil.which("python3") or "python3", str(project_dir / ".atlas_runner.py")],
            cwd=str(project_dir), capture_output=True, text=True, timeout=timeout,
        )

    command = [
        "docker", "run", "--rm", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--memory", settings.code_runner_memory, "--cpus", str(settings.code_runner_cpus),
        "--pids-limit", str(settings.code_runner_pids_limit),
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
        "--user", "10001:10001",
        "--mount", f"type=bind,src={project_dir.resolve()},dst=/workspace,readonly",
        settings.code_runner_image,
    ]
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def runner_health() -> bool:
    if settings.code_runner_backend != "docker":
        return not settings.code_runner_required
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=3)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
