"""Tests for implement-object-storage-and-worker-batch-c (object-storage-and-worker).

Covers the change's acceptance scenarios:
- 6.1 object_storage 磁盘后端：put/get/exists/delete + presign-as-None + 桶映射
- 6.2 object_storage 后端选择：禁用→disk；配置但不可达→降级 disk（不连真实 MinIO）
- 6.3 planner 附件禁用态等价现状：save→read→data_url→delete 一致
- 6.4 worker：known→done；unknown job_type→failed；handler 抛错→failed 可重入
- 6.5 docker-compose.dev.yml 可解析、含七服务、端口不变、依赖表达
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from sqlmodel import Session

from app.core import object_storage as obj
from app.core import planner_attachments as pa
from app.core import job_queue as jq
from app import worker
from app.core.database import engine
from app.models.db import MemoryWritebackJob


# ─── 6.1 / 6.2 object_storage ────────────────────────────────────────────────

class TestObjectStorageDisk:
    def test_put_get_exists_delete(self):
        b = obj.BUCKET_ARTIFACTS
        obj.put_object(b, "k/a.bin", b"data-1", "application/octet-stream")
        assert obj.exists(b, "k/a.bin")
        assert obj.get_object(b, "k/a.bin") == b"data-1"
        obj.delete_object(b, "k/a.bin")
        assert not obj.exists(b, "k/a.bin")
        assert obj.get_object(b, "k/a.bin") is None

    def test_disk_presign_is_none(self):
        obj.put_object(obj.BUCKET_ARTIFACTS, "k/p.bin", b"x")
        assert obj.presign_url(obj.BUCKET_ARTIFACTS, "k/p.bin") is None

    def test_delete_prefix(self):
        b = obj.BUCKET_KNOWLEDGE
        obj.put_object(b, "pfx/1", b"1")
        obj.put_object(b, "pfx/2", b"2")
        obj.delete_prefix(b, "pfx/")
        assert not obj.exists(b, "pfx/1") and not obj.exists(b, "pfx/2")

    def test_bucket_mapping_covers_four(self):
        for logical in (obj.BUCKET_ATTACHMENTS, obj.BUCKET_ARTIFACTS, obj.BUCKET_KNOWLEDGE, obj.BUCKET_EVALUATION):
            assert obj.physical_bucket(logical)  # non-empty mapping

    def test_traversal_guard(self):
        with pytest.raises(ValueError):
            obj._DiskBackend()._path(obj.BUCKET_ARTIFACTS, "../escape")


class TestObjectStorageBackendSelection:
    def test_disabled_uses_disk(self, monkeypatch):
        monkeypatch.setenv("ENABLE_MINIO", "false")
        assert obj._build_backend().name == "disk"

    def test_enabled_no_endpoint_falls_back(self, monkeypatch):
        monkeypatch.setenv("ENABLE_MINIO", "true")
        monkeypatch.delenv("S3_ENDPOINT", raising=False)
        assert obj._build_backend().name == "disk"

    def test_enabled_unreachable_falls_back(self, monkeypatch):
        monkeypatch.setenv("ENABLE_MINIO", "true")
        monkeypatch.setenv("S3_ENDPOINT", "http://127.0.0.1:1")
        assert obj._build_backend().name == "disk"


# ─── 6.3 planner 附件禁用态等价现状 ──────────────────────────────────────────

class TestPlannerAttachments:
    def test_save_read_roundtrip(self):
        meta = pa.save_attachment("cid-att", "doc.txt", b"hello world", "text/plain")
        assert meta["preview_url"] == "/api/planner/sessions/cid-att/attachments/" + meta["stored_name"]
        assert pa.read_attachment_text("cid-att", meta["stored_name"]) == "hello world"
        assert pa.read_attachment_bytes("cid-att", meta["stored_name"]) == b"hello world"

    def test_image_data_url(self):
        meta = pa.save_attachment("cid-att", "p.png", b"\x89PNG\r\n", "image/png")
        url = pa.attachment_data_url("cid-att", meta["stored_name"], "image/png")
        assert url.startswith("data:image/png;base64,")

    def test_dedupe_same_bytes_same_name(self):
        m1 = pa.save_attachment("cid-att", "x.txt", b"same", "text/plain")
        m2 = pa.save_attachment("cid-att", "x.txt", b"same", "text/plain")
        assert m1["stored_name"] == m2["stored_name"]

    def test_delete_session_sweep(self):
        meta = pa.save_attachment("cid-sweep", "y.txt", b"bye", "text/plain")
        assert pa.read_attachment_bytes("cid-sweep", meta["stored_name"]) == b"bye"
        pa.delete_session_attachments("cid-sweep")
        assert pa.read_attachment_bytes("cid-sweep", meta["stored_name"]) is None

    def test_invalid_filename_rejected(self):
        with pytest.raises(ValueError):
            pa.read_attachment_bytes("cid-att", "../escape")


# ─── 6.4 worker ──────────────────────────────────────────────────────────────

class TestWorker:
    def test_known_job_done(self):
        job = jq.enqueue(job_type="extract_profile", source_kind="conversation", source_ref="wk-1")
        assert worker.process_one({"id": job.id, "job_type": "extract_profile"}) is True
        with Session(engine) as s:
            assert s.get(MemoryWritebackJob, job.id).status == "done"

    def test_unknown_job_type_failed_not_done(self):
        job = jq.enqueue(job_type="extract_episodic", source_kind="proposal", source_ref="wk-2")
        assert worker.process_one({"id": job.id, "job_type": "totally-unknown"}) is False
        with Session(engine) as s:
            assert s.get(MemoryWritebackJob, job.id).status == "failed"

    def test_handler_failure_then_reenqueue(self):
        def boom(job_id, payload):
            raise RuntimeError("kaboom")

        worker.register_handler("merge_semantic", boom)
        try:
            job = jq.enqueue(job_type="merge_semantic", source_kind="run_summary", source_ref="wk-3")
            assert worker.process_one({"id": job.id, "job_type": "merge_semantic"}) is False
            with Session(engine) as s:
                row = s.get(MemoryWritebackJob, job.id)
                assert row.status == "failed" and row.error
            # failed is terminal for dedupe → re-enqueue creates a fresh job
            job2 = jq.enqueue(job_type="merge_semantic", source_kind="run_summary", source_ref="wk-3")
            assert job2.id != job.id
        finally:
            worker.register_handler("merge_semantic", worker._placeholder("merge_semantic"))

    def test_envelope_without_id_dropped(self):
        assert worker.process_one({"job_type": "extract_profile"}) is False


# ─── 6.5 docker-compose ──────────────────────────────────────────────────────

class TestDockerCompose:
    def _load(self) -> dict:
        root = Path(__file__).resolve().parents[2]
        return yaml.safe_load((root / "docker-compose.dev.yml").read_text())

    def test_has_seven_core_services(self):
        svcs = self._load()["services"]
        for name in ("frontend", "backend", "worker", "postgres", "redis", "minio", "mem0"):
            assert name in svcs

    def test_ports_unchanged(self):
        svcs = self._load()["services"]
        assert "3000:3000" in svcs["frontend"]["ports"]
        assert "8000:8000" in svcs["backend"]["ports"]

    def test_backend_and_worker_depend_on_infra(self):
        svcs = self._load()["services"]
        for svc in ("backend", "worker"):
            deps = svcs[svc]["depends_on"]
            for infra in ("postgres", "redis", "minio"):
                assert infra in deps
