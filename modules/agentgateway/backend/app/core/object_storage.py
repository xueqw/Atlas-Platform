"""Object storage facade with a transparent local-disk fallback (Batch C).

A single entry point for binary assets (planner attachments now; agent artifacts
/ knowledge / evaluation reports later). The concrete backend is chosen ONCE at
import time:

  - ``ENABLE_MINIO=true`` and the endpoint is reachable → :class:`_MinioBackend`.
  - otherwise (disabled, ``minio`` not installed, or probe fails) →
    :class:`_DiskBackend`, logging a WARNING in the configured-but-unreachable case.

Callers never branch on ``ENABLE_MINIO``: both backends expose the same verbs,
so disabling MinIO is byte-for-byte the previous on-disk behaviour. Four logical
buckets (planner-attachments / agent-artifacts / knowledge-assets /
evaluation-reports) map to physical buckets via ``S3_BUCKET_*``.

A single runtime call that raises is swallowed and degraded — object storage
must never take down the main request path.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Optional

# Importing config loads backend/.env into os.environ (idempotent).
from app.core import config as _config  # noqa: F401

_log = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# Logical bucket ids (stable across code) → physical bucket env vars.
BUCKET_ATTACHMENTS = "planner-attachments"
BUCKET_ARTIFACTS = "agent-artifacts"
BUCKET_KNOWLEDGE = "knowledge-assets"
BUCKET_EVALUATION = "evaluation-reports"
BUCKET_SKILL_BUNDLES = "skill-bundles"

_BUCKET_ENV = {
    BUCKET_ATTACHMENTS: "S3_BUCKET_ATTACHMENTS",
    BUCKET_ARTIFACTS: "S3_BUCKET_ARTIFACTS",
    BUCKET_KNOWLEDGE: "S3_BUCKET_KNOWLEDGE",
    BUCKET_EVALUATION: "S3_BUCKET_EVALUATION",
    BUCKET_SKILL_BUNDLES: "S3_BUCKET_SKILL_BUNDLES",
}


def physical_bucket(logical: str) -> str:
    """Resolve a logical bucket id to its configured physical bucket name."""
    env = _BUCKET_ENV.get(logical)
    if env is None:
        return logical  # unknown logical id: use as-is
    return (os.environ.get(env) or logical).strip()


# Local-disk root for the fallback backend.
_DISK_ROOT = Path(__file__).resolve().parents[2] / "data" / "object-store"


class _DiskBackend:
    """Local-disk fallback: ``data/object-store/{physical-bucket}/{key}``.

    presign returns the API proxy path (the existing attachment-serving route),
    preserving the previous ``preview_url`` semantics when MinIO is disabled.
    """

    def __init__(self) -> None:
        self.name = "disk"

    def _path(self, bucket: str, key: str) -> Path:
        if ".." in key:
            raise ValueError(f"invalid key: {key!r}")
        return _DISK_ROOT / physical_bucket(bucket) / key

    def put_object(self, bucket: str, key: str, data: bytes, content_type: str = "") -> None:
        p = self._path(bucket, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get_object(self, bucket: str, key: str) -> Optional[bytes]:
        p = self._path(bucket, key)
        if not p.exists() or not p.is_file():
            return None
        return p.read_bytes()

    def exists(self, bucket: str, key: str) -> bool:
        return self._path(bucket, key).is_file()

    def delete_object(self, bucket: str, key: str) -> None:
        p = self._path(bucket, key)
        if p.is_file():
            p.unlink()

    def delete_prefix(self, bucket: str, prefix: str) -> None:
        base = self._path(bucket, prefix)
        if base.is_dir():
            shutil.rmtree(base, ignore_errors=True)

    def presign_url(self, bucket: str, key: str, ttl: int = 3600) -> Optional[str]:
        # Disk backend has no signed URL; callers fall back to the API proxy route.
        return None

    def health_check(self) -> bool:
        return True


class _MinioBackend:
    """MinIO / S3-compatible backend. Buckets are created on init if absent."""

    def __init__(self, client) -> None:
        self.name = "minio"
        self._c = client
        # Ensure the four physical buckets exist (idempotent, best-effort).
        for logical in (BUCKET_ATTACHMENTS, BUCKET_ARTIFACTS, BUCKET_KNOWLEDGE, BUCKET_EVALUATION, BUCKET_SKILL_BUNDLES):
            b = physical_bucket(logical)
            try:
                if not client.bucket_exists(b):
                    client.make_bucket(b)
            except Exception as exc:  # don't let bucket setup break startup
                _log.warning("object_storage: ensure bucket %s failed (%s)", b, exc)

    def put_object(self, bucket: str, key: str, data: bytes, content_type: str = "") -> None:
        import io

        self._c.put_object(
            physical_bucket(bucket), key, io.BytesIO(data), length=len(data),
            content_type=content_type or "application/octet-stream",
        )

    def get_object(self, bucket: str, key: str) -> Optional[bytes]:
        try:
            resp = self._c.get_object(physical_bucket(bucket), key)
        except Exception:
            return None
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    def exists(self, bucket: str, key: str) -> bool:
        try:
            self._c.stat_object(physical_bucket(bucket), key)
            return True
        except Exception:
            return False

    def delete_object(self, bucket: str, key: str) -> None:
        self._c.remove_object(physical_bucket(bucket), key)

    def delete_prefix(self, bucket: str, prefix: str) -> None:
        b = physical_bucket(bucket)
        try:
            from minio.deleteobjects import DeleteObject

            names = [DeleteObject(o.object_name) for o in self._c.list_objects(b, prefix=prefix, recursive=True)]
            if names:
                # remove_objects returns an iterator of errors; drain it.
                for _ in self._c.remove_objects(b, names):
                    pass
        except Exception as exc:
            _log.warning("object_storage: delete_prefix %s/%s failed (%s)", b, prefix, exc)

    def presign_url(self, bucket: str, key: str, ttl: int = 3600) -> Optional[str]:
        from datetime import timedelta

        try:
            return self._c.presigned_get_object(physical_bucket(bucket), key, expires=timedelta(seconds=ttl))
        except Exception as exc:
            _log.warning("object_storage: presign %s/%s failed (%s)", bucket, key, exc)
            return None

    def health_check(self) -> bool:
        try:
            self._c.list_buckets()
            return True
        except Exception:
            return False


def _build_backend():
    """Pick the backend once: MinIO when enabled AND reachable, else disk."""
    if not _env_bool("ENABLE_MINIO", False):
        return _DiskBackend()
    endpoint = (os.environ.get("S3_ENDPOINT") or "").strip()
    if not endpoint:
        _log.warning("ENABLE_MINIO=true but S3_ENDPOINT is empty; falling back to disk backend")
        return _DiskBackend()
    try:
        from minio import Minio

        # Strip scheme; MinIO client takes host:port + secure flag.
        secure = endpoint.startswith("https://") or _env_bool("S3_USE_SSL", False)
        host = endpoint.split("://", 1)[-1]
        client = Minio(
            host,
            access_key=os.environ.get("S3_ACCESS_KEY") or None,
            secret_key=os.environ.get("S3_SECRET_KEY") or None,
            secure=secure,
            region=os.environ.get("S3_REGION") or None,
        )
        client.list_buckets()  # probe
        _log.info("object_storage: connected to MinIO at %s", host)
        return _MinioBackend(client)
    except Exception as exc:
        _log.warning(
            "object_storage: MinIO unavailable (%s: %s); falling back to disk backend",
            type(exc).__name__, exc,
        )
        return _DiskBackend()


_backend = _build_backend()


def _set_backend_for_test(backend) -> None:
    """Test seam: swap the active backend."""
    global _backend
    _backend = backend


def backend_name() -> str:
    return _backend.name


# ── Facade ──────────────────────────────────────────────────────────────────

def put_object(bucket: str, key: str, data: bytes, content_type: str = "") -> bool:
    try:
        _backend.put_object(bucket, key, data, content_type=content_type)
        return True
    except Exception as exc:
        _log.warning("put_object(%s/%s) failed (%s)", bucket, key, exc)
        return False


def get_object(bucket: str, key: str) -> Optional[bytes]:
    try:
        return _backend.get_object(bucket, key)
    except Exception as exc:
        _log.warning("get_object(%s/%s) failed (%s)", bucket, key, exc)
        return None


def exists(bucket: str, key: str) -> bool:
    try:
        return _backend.exists(bucket, key)
    except Exception:
        return False


def delete_object(bucket: str, key: str) -> None:
    try:
        _backend.delete_object(bucket, key)
    except Exception as exc:
        _log.warning("delete_object(%s/%s) failed (%s)", bucket, key, exc)


def delete_prefix(bucket: str, prefix: str) -> None:
    try:
        _backend.delete_prefix(bucket, prefix)
    except Exception as exc:
        _log.warning("delete_prefix(%s/%s) failed (%s)", bucket, prefix, exc)


def presign_url(bucket: str, key: str, ttl: int = 3600) -> Optional[str]:
    try:
        return _backend.presign_url(bucket, key, ttl=ttl)
    except Exception as exc:
        _log.warning("presign_url(%s/%s) failed (%s)", bucket, key, exc)
        return None


def health_check() -> bool:
    try:
        return _backend.health_check()
    except Exception:
        return False
