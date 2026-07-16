"""Backend tests for change: agency-agents-minio-ingestion (T10).

Drives the ingestion pipeline with a FakeFetcher (offline) + the disk object-
storage backend (conftest pins ENABLE_MINIO=false). Covers spec
agency-agents-asset-ingestion + the expert-template-retrieval provenance delta:
- raw md stored in object storage, retrievable by key
- parse/classify routes md to the right asset_type; docs not indexed
- expert_template indexed with provenance, config has no raw md full-text
- observability: ingest run + per-stage events; per-file failure degrades
- idempotent re-sync; imported template hit by T3 retrieval (no raw read)
"""

from __future__ import annotations

import json

import pytest
from sqlmodel import Session, select

from app.api import planner as planner_api
from app.core import asset_ingestion as ai
from app.core import object_storage as obj
from app.core.expert_retrieval import retrieve_expert_candidates
from app.models.db import AssetEvent, AssetIngestRun, CapabilityItem


_SALES_MD = """---
name: imported-sales-analyst
domain: sales
deliverables:
  - 销售日报
  - 客户分层
recommended_modes:
  - cron
recommended_capability_tags:
  - sales
  - analysis
tags:
  - sales
---

# 销售管道分析专家

面向销售团队的管道分析专家，强调结论先行、可执行建议。

## 详细方法论

SECRET_RAW_BODY: 这是靠后的长篇原文正文，只应留在对象存储里，不应整段进入 config。
"""

_DOCS_MD = """# README

集成说明文档，不应进入 expert_template 检索索引。
"""

_WORKFLOW_MD = """---
name: report-runbook
---

# 报告生成 runbook

流程说明。
"""


def _fetcher():
    return ai.FakeFetcher({
        "experts/sales.md": _SALES_MD,
        "docs/README.md": _DOCS_MD,
        "strategy/playbooks/report.md": _WORKFLOW_MD,
    })


def _run(version="v-test", session=None):
    return ai.run_ingestion(
        source_repo="gitee.com/fengpiaoyao/agency-agents",
        source_version=version, fetcher=_fetcher(), session=session,
    )


# ─── 8.1 raw stored + retrievable ───────────────────────────────────────────

def test_raw_md_stored_and_retrievable():
    res = _run()
    assert res["status"] == "completed"
    key = "agency-agents/raw/experts/sales.md"
    raw = obj.get_object(obj.BUCKET_KNOWLEDGE, key)
    assert raw is not None and b"SECRET_RAW_BODY" in raw  # full text lives ONLY in storage


# ─── 8.2 classify ───────────────────────────────────────────────────────────

def test_classify_routes_assets():
    assert ai.classify_asset("docs/README.md", _DOCS_MD, {}) == ai.ASSET_DOCS
    assert ai.classify_asset("strategy/playbooks/report.md", _WORKFLOW_MD, {}) == ai.ASSET_WORKFLOW_BLUEPRINT
    assert ai.classify_asset("experts/sales.md", _SALES_MD, {}) == ai.ASSET_EXPERT_TEMPLATE


def test_docs_not_indexed():
    _run()
    with Session(planner_api.engine) as s:
        rows = s.exec(select(CapabilityItem).where(
            CapabilityItem.type == "expert_template",
            CapabilityItem.name == "README",
        )).all()
    assert rows == []  # docs class never enters the expert_template index


# ─── 8.3 index with provenance, no raw in config ────────────────────────────

def test_expert_template_indexed_with_provenance_no_raw():
    _run(version="v-prov")
    with Session(planner_api.engine) as s:
        row = s.exec(select(CapabilityItem).where(
            CapabilityItem.type == "expert_template",
            CapabilityItem.name == "imported-sales-analyst",
        )).first()
    assert row is not None
    cfg = json.loads(row.config)
    assert cfg["domain"] == "sales"
    assert cfg["source_repo"] == "gitee.com/fengpiaoyao/agency-agents"
    assert cfg["source_path"] == "experts/sales.md"
    assert cfg["minio_object_key"] == "agency-agents/raw/experts/sales.md"
    assert cfg["source_version"] == "v-prov"
    # raw markdown body must NOT be inlined into config
    assert "SECRET_RAW_BODY" not in row.config


# ─── 8.4 observability + per-file failure degrade ───────────────────────────

def test_ingest_run_and_events_recorded():
    res = _run()
    rid = res["run_id"]
    with Session(planner_api.engine) as s:
        run = s.get(AssetIngestRun, rid)
        assert run is not None and run.status == "completed"
        events = s.exec(select(AssetEvent).where(AssetEvent.ingest_run_id == rid)).all()
    etypes = {e.event_type for e in events}
    assert {"fetch", "store", "classify", "index"} <= etypes


def test_per_file_failure_degrades_not_abort():
    class BadFetcher:
        def list_tree(self):
            return [ai.SourceFile(source_path="experts/ok.md"),
                    ai.SourceFile(source_path="experts/bad.md")]

        def fetch_raw(self, source_path):
            if source_path.endswith("bad.md"):
                raise RuntimeError("network boom")
            return _SALES_MD

    res = ai.run_ingestion(source_version="v-degrade", fetcher=BadFetcher())
    assert res["status"] == "completed"  # batch finishes despite one failure
    assert res["counts"]["failed"] >= 1
    with Session(planner_api.engine) as s:
        failed = s.exec(select(AssetEvent).where(
            AssetEvent.ingest_run_id == res["run_id"], AssetEvent.status == "failed"
        )).all()
    assert failed and any("bad.md" in e.source_path for e in failed)


# ─── 8.5 idempotent re-sync ─────────────────────────────────────────────────

def test_idempotent_resync():
    _run(version="v1")
    _run(version="v2")
    with Session(planner_api.engine) as s:
        rows = s.exec(select(CapabilityItem).where(
            CapabilityItem.type == "expert_template",
            CapabilityItem.name == "imported-sales-analyst",
        )).all()
        runs = s.exec(select(AssetIngestRun)).all()
    assert len(rows) == 1  # upsert, not duplicate
    assert len(runs) >= 2  # each sync logs its own run


# ─── 8.6 retrieval hit (no raw read) ────────────────────────────────────────

def test_imported_template_hit_by_retrieval():
    _run()
    cands = retrieve_expert_candidates(goal_text="做一个 sales 销售分析 agent")
    assert any(c["name"] == "imported-sales-analyst" for c in cands)
    # retrieval candidates never carry raw markdown
    assert "SECRET_RAW_BODY" not in json.dumps(cands)
