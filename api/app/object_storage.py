from __future__ import annotations

from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from minio import Minio

from .config import settings


def _safe_part(value: str) -> str:
    return quote(value.strip().replace("/", "_"), safe="-_.")[:240]


def knowledge_object_key(workspace_id: str, knowledge_base_id: str, document_id: str, filename: str) -> str:
    return f"workspaces/{_safe_part(workspace_id)}/knowledge/{_safe_part(knowledge_base_id)}/{_safe_part(document_id)}/{_safe_part(filename)}"


def agent_archive_key(workspace_id: str, agent_id: str, version_no: int) -> str:
    return f"workspaces/{_safe_part(workspace_id)}/agents/{_safe_part(agent_id)}/versions/{version_no}.json"


class ObjectStorage:
    def __init__(self) -> None:
        self.backend = settings.object_storage_backend.lower()
        self.root = Path(settings.local_storage_root).resolve()
        self._client: Minio | None = None

    def _minio(self) -> Minio:
        if self._client is None:
            self._client = Minio(
                settings.object_storage_endpoint,
                access_key=settings.object_storage_access_key,
                secret_key=settings.object_storage_secret_key,
                secure=settings.object_storage_secure,
            )
        return self._client

    def ensure_ready(self) -> None:
        if self.backend == "minio":
            client = self._minio()
            if not client.bucket_exists(settings.object_storage_bucket):
                client.make_bucket(settings.object_storage_bucket)
        elif self.backend == "local":
            self.root.mkdir(parents=True, exist_ok=True)
        else:
            raise RuntimeError(f"Unsupported object storage backend: {self.backend}")

    def put(self, key: str, payload: bytes, content_type: str = "application/octet-stream") -> None:
        self.ensure_ready()
        if self.backend == "minio":
            self._minio().put_object(
                settings.object_storage_bucket, key, BytesIO(payload), len(payload), content_type=content_type,
            )
            return
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    def get(self, key: str) -> bytes:
        if self.backend == "minio":
            response = self._minio().get_object(settings.object_storage_bucket, key)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()
        return (self.root / key).read_bytes()

    def delete(self, key: str) -> None:
        if not key:
            return
        if self.backend == "minio":
            self._minio().remove_object(settings.object_storage_bucket, key)
            return
        (self.root / key).unlink(missing_ok=True)

    def ping(self) -> bool:
        try:
            self.ensure_ready()
            if self.backend == "minio":
                self._minio().list_buckets()
            return True
        except Exception:
            return False


object_storage = ObjectStorage()
