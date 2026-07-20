"""Real Docker release probe for the code-agent sandbox.

This file is intentionally skipped in the normal unit suite. Release hosts run
it with ``ATLAS_RUN_DOCKER_ACCEPTANCE=1`` after building the configured runner
image. Unlike the command-construction unit test, this probe inspects a live
container and verifies that timeout cleanup reaches the Docker daemon.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from app import code_runner


pytestmark = pytest.mark.skipif(
    os.getenv("ATLAS_RUN_DOCKER_ACCEPTANCE") != "1",
    reason="set ATLAS_RUN_DOCKER_ACCEPTANCE=1 for the real Docker release probe",
)


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=15, check=False,
    )


def _runner_container_ids() -> set[str]:
    result = _docker("ps", "-aq", "--filter", "label=com.atlas.runner=true")
    assert result.returncode == 0, result.stderr
    return set(result.stdout.split())


def _wait_for_new_container(before: set[str], timeout: float = 10) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        created = _runner_container_ids() - before
        if created:
            assert len(created) == 1, f"unexpected concurrent Atlas runners: {sorted(created)}"
            return created.pop()
        time.sleep(0.1)
    raise AssertionError("Atlas runner container did not become observable")


def _wait_until_removed(container_ids: set[str], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not (_runner_container_ids() & container_ids):
            return
        time.sleep(0.1)
    raise AssertionError(f"Atlas runner containers leaked: {sorted(_runner_container_ids() & container_ids)}")


def _probe_source() -> str:
    return r'''import json
import os
from pathlib import Path
import socket
import time


def write_is_denied(path):
    try:
        Path(path).write_text("forbidden", encoding="utf-8")
    except OSError:
        return True
    return False


try:
    with socket.create_connection(("1.1.1.1", 53), timeout=0.5):
        network_blocked = False
except OSError:
    network_blocked = True

tmp_path = Path("/tmp/atlas-probe")
try:
    tmp_path.write_text("allowed", encoding="utf-8")
    tmp_writable = tmp_path.read_text(encoding="utf-8") == "allowed"
finally:
    tmp_path.unlink(missing_ok=True)

status = {}
for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
    if ":" in line:
        key, value = line.split(":", 1)
        status[key] = value.strip()

secret_names = {
    "ATLAS_RELEASE_PROBE_SECRET", "DATABASE_URL", "REDIS_URL",
    "OPENAI_API_KEY", "ZHIPU_API_KEY", "SILICONFLOW_API_KEY",
    "OBJECT_STORAGE_SECRET_KEY", "SECRET_ENCRYPTION_KEY",
}
result = {
    "network_blocked": network_blocked,
    "rootfs_readonly": write_is_denied("/atlas-root-probe"),
    "workspace_readonly": write_is_denied("/workspace/atlas-workspace-probe"),
    "tmp_writable": tmp_writable,
    "uid": os.getuid(),
    "cap_eff": status.get("CapEff", ""),
    "no_new_privs": status.get("NoNewPrivs", ""),
    "secret_env": sorted(secret_names.intersection(os.environ)),
}
print(json.dumps(result), flush=True)
time.sleep(3)
'''


def test_real_docker_sandbox_and_timeout_cleanup(tmp_path: Path, monkeypatch):
    info = _docker("info")
    assert info.returncode == 0, f"Docker daemon unavailable: {info.stderr}"

    image = os.getenv("ATLAS_DOCKER_ACCEPTANCE_IMAGE", "atlas-agent-runner:py311")
    image_check = _docker("image", "inspect", image)
    assert image_check.returncode == 0, f"build the runner image first: {image_check.stderr}"
    assert getattr(os, "getuid", lambda: 1000)() != 0, "release host must run Atlas as non-root"

    monkeypatch.setattr(code_runner.settings, "environment", "production")
    monkeypatch.setattr(code_runner.settings, "code_runner_backend", "docker")
    monkeypatch.setattr(code_runner.settings, "code_runner_required", True)
    monkeypatch.setattr(code_runner.settings, "code_runner_image", image)
    monkeypatch.setattr(code_runner.settings, "code_runner_memory", "128m")
    monkeypatch.setattr(code_runner.settings, "code_runner_cpus", 0.25)
    monkeypatch.setattr(code_runner.settings, "code_runner_pids_limit", 32)
    monkeypatch.setenv("ATLAS_RELEASE_PROBE_SECRET", "must-not-enter-container")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-enter-container")

    runner_file = tmp_path / ".atlas_runner.py"
    runner_file.write_text(_probe_source(), encoding="utf-8")
    before = _runner_container_ids()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(code_runner.run_python, tmp_path, 15)
        container_id = _wait_for_new_container(before)
        inspected = _docker("inspect", container_id)
        assert inspected.returncode == 0, inspected.stderr
        container = json.loads(inspected.stdout)[0]

        host = container["HostConfig"]
        assert host["NetworkMode"] == "none"
        assert host["ReadonlyRootfs"] is True
        assert {value.upper() for value in host["CapDrop"]} == {"ALL"}
        assert "no-new-privileges" in host["SecurityOpt"]
        assert host["Memory"] == 128 * 1024 * 1024
        assert host["MemorySwap"] == 128 * 1024 * 1024
        assert host["NanoCpus"] == 250_000_000
        assert host["PidsLimit"] == 32
        assert host["Init"] is True
        assert int(container["Config"]["User"].split(":", 1)[0]) != 0
        workspace = next(item for item in container["Mounts"] if item["Destination"] == "/workspace")
        assert workspace["RW"] is False
        configured_env = {item.split("=", 1)[0] for item in container["Config"]["Env"]}
        assert "ATLAS_RELEASE_PROBE_SECRET" not in configured_env
        assert "OPENAI_API_KEY" not in configured_env

        completed = future.result(timeout=20)

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["network_blocked"] is True
    assert report["rootfs_readonly"] is True
    assert report["workspace_readonly"] is True
    assert report["tmp_writable"] is True
    assert report["uid"] != 0
    assert int(report["cap_eff"], 16) == 0
    assert report["no_new_privs"] == "1"
    assert report["secret_env"] == []
    _wait_until_removed({container_id})

    runner_file.write_text("while True: pass\n", encoding="utf-8")
    before_timeout = _runner_container_ids()
    with pytest.raises(subprocess.TimeoutExpired):
        code_runner.run_python(tmp_path, 2)
    _wait_until_removed(_runner_container_ids() - before_timeout)
    assert _runner_container_ids() == before_timeout
