from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from .config import settings


def _runner_identity() -> tuple[int, int]:
    """Choose a non-root Linux identity for Docker Desktop hosts too.

    Windows has no ``os.getuid``/``getgid``. Docker Desktop still launches a
    Linux container, where the conventional unprivileged UID/GID 1000 is safer
    than silently dropping the ``--user`` restriction.
    """
    return getattr(os, "getuid", lambda: 1000)(), getattr(os, "getgid", lambda: 1000)()


def _run_local(project_dir: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(project_dir / ".atlas_runner.py")],
        cwd=str(project_dir), capture_output=True, text=True, timeout=timeout,
    )


def _may_fallback_to_local() -> bool:
    return not settings.code_runner_required and settings.environment != "production"


def _remove_container(*, name: str, cidfile: Path) -> None:
    """Best-effort removal for a Docker run interrupted before ``--rm`` fires.

    Killing the local ``docker run`` client does not necessarily stop the
    daemon-managed container. Prefer the cid recorded by Docker, while keeping
    the unique name as a fallback for failures before the cidfile is written.
    """

    identifier = name
    try:
        candidate = cidfile.read_text(encoding="utf-8").strip()
        if candidate:
            identifier = candidate
    except OSError:
        pass
    try:
        subprocess.run(
            ["docker", "rm", "-f", identifier],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Preserve the original runner exception. Health checks and the release
        # probe surface a daemon that cannot complete forced cleanup.
        pass


def run_python(project_dir: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    if settings.code_runner_backend != "docker":
        if settings.code_runner_required or settings.environment == "production":
            raise RuntimeError("Production code execution requires Docker runner")
        return _run_local(project_dir, timeout)

    runner_uid, runner_gid = _runner_identity()
    if runner_uid == 0:
        raise RuntimeError("Atlas service must not run code-agent containers as root")

    container_name = f"atlas-runner-{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory(prefix="atlas-runner-control-") as control_dir:
        cidfile = Path(control_dir) / "container.cid"
        command = [
            "docker", "run", "--rm", "--name", container_name,
            "--cidfile", str(cidfile), "--label", "com.atlas.runner=true",
            "--init", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", settings.code_runner_memory,
            "--memory-swap", settings.code_runner_memory,
            "--cpus", str(settings.code_runner_cpus),
            "--pids-limit", str(settings.code_runner_pids_limit),
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
            "--user", f"{runner_uid}:{runner_gid}",
            "--mount", f"type=bind,src={project_dir.resolve()},dst=/workspace,readonly",
            settings.code_runner_image,
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            _remove_container(name=container_name, cidfile=cidfile)
            raise
        except OSError:
            _remove_container(name=container_name, cidfile=cidfile)
            if _may_fallback_to_local():
                return _run_local(project_dir, timeout)
            raise
        except BaseException:
            _remove_container(name=container_name, cidfile=cidfile)
            raise
    if result.returncode and _may_fallback_to_local() and any(
        marker in (result.stderr or "").lower()
        for marker in ("cannot connect", "no such image", "not recognized", "is not running")
    ):
        return _run_local(project_dir, timeout)
    return result


def runner_health() -> bool:
    if settings.code_runner_backend != "docker":
        return not settings.code_runner_required
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=3)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
