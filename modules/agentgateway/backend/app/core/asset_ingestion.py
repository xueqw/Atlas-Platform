"""Cognitive-asset ingestion (T10: agency-agents-minio-ingestion).

Imports an external expert-asset repo (default: agency-agents on Gitee) into the
platform as structured ``expert_template`` capabilities, keeping raw markdown in
object storage (MinIO/disk) — never inlining it into runtime.

Pipeline (``run_ingestion``):  fetch → store(MinIO) → parse → classify → index,
emitting one AssetEvent per file-stage and bracketing the batch with an
AssetIngestRun. The fetcher is pluggable (``AssetFetcher``): ``GiteeFetcher``
does real HTTP; ``FakeFetcher`` feeds in-memory content so the chain is testable
offline. A single file failing degrades to a ``failed`` event and is skipped —
the batch continues.

Design refs: D1 (pluggable fetcher), D2 (reuse object_storage, key scheme),
D3 (parse/classify heuristic), D5 (idempotent index + provenance, no raw in
config). The planner reads the structured index (T3 retrieval), never MinIO.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Protocol

from sqlmodel import Session, select

from app.core import object_storage as obj
from app.core.database import engine
from app.models.db import AssetEvent, AssetIngestRun, CapabilityItem, _utcnow

_log = logging.getLogger(__name__)

# Default source config (D7). Overridable via env; offline tests inject a fetcher.
DEFAULT_SOURCE_NAME = "agency-agents"
DEFAULT_SOURCE_REPO = os.environ.get(
    "AGENCY_AGENTS_REPO", "gitee.com/fengpiaoyao/agency-agents"
)
DEFAULT_BRANCH = os.environ.get("AGENCY_AGENTS_BRANCH", "master")
RAW_PREFIX = "agency-agents/raw"
MANIFEST_PREFIX = "agency-agents/manifests"

# Asset classes (D3). docs do NOT enter the planner retrieval pool.
ASSET_EXPERT_TEMPLATE = "expert_template"
ASSET_WORKFLOW_BLUEPRINT = "workflow_blueprint"
ASSET_PROMPT_TEMPLATE = "prompt_template"
ASSET_DOCS = "docs"


# ── Fetcher interface (D1) ───────────────────────────────────────────────────

@dataclass
class SourceFile:
    """One file discovered in the source repo tree."""
    source_path: str          # path within the repo, e.g. "marketing/xxx.md"
    name: str = ""            # logical name (slug); derived if empty


class AssetFetcher(Protocol):
    """Pluggable source. ``list_tree`` enumerates md files; ``fetch_raw`` reads
    one file's bytes. Implementations raise on hard failure (caught per-file)."""

    def list_tree(self) -> List[SourceFile]: ...

    def fetch_raw(self, source_path: str) -> str: ...


class FakeFetcher:
    """In-memory fetcher for tests/offline: built from ``{source_path: content}``."""

    def __init__(self, files: Dict[str, str]):
        self._files = dict(files)

    def list_tree(self) -> List[SourceFile]:
        return [SourceFile(source_path=p) for p in sorted(self._files)]

    def fetch_raw(self, source_path: str) -> str:
        if source_path not in self._files:
            raise KeyError(f"no such file: {source_path}")
        return self._files[source_path]


class GiteeFetcher:
    """Real Gitee fetcher (httpx). Walks the repo tree recursively via the Gitee
    v5 contents API and fetches raw md. Network-dependent; never used in tests."""

    def __init__(self, repo: str = DEFAULT_SOURCE_REPO, branch: str = DEFAULT_BRANCH,
                 token: str = "", timeout: float = 15.0):
        # repo "gitee.com/owner/name" → owner/name
        self.owner_name = repo.split("gitee.com/", 1)[-1].strip("/")
        self.branch = branch
        self.token = token or os.environ.get("GITEE_TOKEN", "")
        self.timeout = timeout

    def _api(self, path: str) -> str:
        base = f"https://gitee.com/api/v5/repos/{self.owner_name}/contents/{path}".rstrip("/")
        return base

    def list_tree(self) -> List[SourceFile]:
        import httpx

        out: List[SourceFile] = []

        def _walk(path: str) -> None:
            params = {"ref": self.branch}
            if self.token:
                params["access_token"] = self.token
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(self._api(path), params=params)
                resp.raise_for_status()
                entries = resp.json()
            for e in entries if isinstance(entries, list) else []:
                etype, epath = e.get("type"), e.get("path", "")
                if etype == "dir":
                    _walk(epath)
                elif etype == "file" and epath.lower().endswith(".md"):
                    out.append(SourceFile(source_path=epath))

        _walk("")
        return out

    def fetch_raw(self, source_path: str) -> str:
        import httpx

        url = f"https://gitee.com/{self.owner_name}/raw/{self.branch}/{source_path}"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text


# ── Parse / classify (D3) ────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_frontmatter(md: str) -> Dict[str, object]:
    """Best-effort YAML-ish frontmatter parse (no external dep): ``key: value``
    lines and ``- item`` lists. Tolerant — returns {} when absent/garbled."""
    m = _FRONTMATTER_RE.match(md or "")
    if not m:
        return {}
    out: Dict[str, object] = {}
    cur_key: Optional[str] = None
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        if re.match(r"^\s*-\s+", line) and cur_key:
            out.setdefault(cur_key, [])
            if isinstance(out[cur_key], list):
                out[cur_key].append(line.split("-", 1)[1].strip().strip("\"'"))
            continue
        mk = re.match(r"^([A-Za-z0-9_]+)\s*:\s*(.*)$", line)
        if mk:
            key, val = mk.group(1), mk.group(2).strip()
            cur_key = key
            if val:
                out[key] = val.strip("\"'")
            else:
                out[key] = []
    return out


def _first_heading(md: str) -> str:
    for line in (md or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()
    return ""


def _slug_from_path(source_path: str) -> str:
    base = source_path.rsplit("/", 1)[-1]
    return re.sub(r"\.md$", "", base, flags=re.IGNORECASE)


def classify_asset(source_path: str, md: str, fm: Dict[str, object]) -> str:
    """Heuristic asset classification (D3). Path/title keywords + frontmatter."""
    p = source_path.lower()
    if any(k in p for k in ("readme", "/docs/", "integration", "集成", "说明")):
        return ASSET_DOCS
    if any(k in p for k in ("playbook", "runbook", "workflow", "流程", "strategy/")):
        return ASSET_WORKFLOW_BLUEPRINT
    if any(k in p for k in ("activation", "handoff", "coordination", "prompt")):
        return ASSET_PROMPT_TEMPLATE
    # Default: a role/business expert markdown.
    return ASSET_EXPERT_TEMPLATE


def parse_expert_template(source_path: str, md: str, fm: Dict[str, object]) -> Dict[str, object]:
    """Produce a T3-shaped expert_template config from a parsed md (D3/D5).

    Structured metadata + a persona SUMMARY only — never the full markdown. The
    caller adds provenance (source_repo/source_path/minio_object_key/version)."""
    def _as_list(v) -> List[str]:
        if isinstance(v, list):
            return [str(x) for x in v]
        if isinstance(v, str) and v.strip():
            return [s.strip() for s in re.split(r"[,，;；]", v) if s.strip()]
        return []

    name = str(fm.get("name") or "").strip() or _slug_from_path(source_path)
    domain = str(fm.get("domain") or "").strip()
    if not domain:
        # infer from top-level directory
        domain = source_path.split("/", 1)[0] if "/" in source_path else ""
    description = str(fm.get("description") or "").strip() or _first_heading(md)
    # persona summary: first non-empty prose paragraph after frontmatter, capped.
    body = _FRONTMATTER_RE.sub("", md or "", count=1).strip()
    persona = ""
    for para in re.split(r"\n\s*\n", body):
        t = para.strip()
        if t and not t.startswith("#"):
            persona = t[:300]
            break
    cfg: Dict[str, object] = {
        "domain": domain,
        "subdomain": str(fm.get("subdomain") or ""),
        "deliverables": _as_list(fm.get("deliverables")),
        "workflow_hints": _as_list(fm.get("workflow_hints")),
        "recommended_modes": _as_list(fm.get("recommended_modes")),
        "recommended_capability_tags": _as_list(fm.get("recommended_capability_tags") or fm.get("tags")),
        "persona_summary": persona,
    }
    return {"name": name, "description": description[:2000], "config": cfg,
            "tags": _as_list(fm.get("tags"))}


# ── Event + index helpers ────────────────────────────────────────────────────

def _record_event(s: Session, *, run_id: Optional[int], event_type: str,
                  status: str = "success", source_path: str = "", object_key: str = "",
                  asset_type: str = "", message: str = "", details: Optional[dict] = None) -> None:
    """Persist one AssetEvent (best-effort — observability must not break ingest)."""
    try:
        s.add(AssetEvent(
            ingest_run_id=run_id, source_name=DEFAULT_SOURCE_NAME,
            event_type=event_type, status=status, source_path=source_path,
            object_key=object_key, asset_type=asset_type, message=message[:2000],
            details_json=json.dumps(details or {}, ensure_ascii=False),
        ))
        s.commit()
    except Exception as exc:
        _log.warning("asset event record failed (%s)", exc)
        s.rollback()


def _index_expert_template(s: Session, parsed: dict, provenance: dict) -> bool:
    """Idempotent upsert of an expert_template CapabilityItem (D5).

    config = parsed structured metadata + provenance; NEVER the raw markdown.
    Dedup by (type="expert_template", name)."""
    name = parsed["name"]
    cfg = dict(parsed["config"])
    cfg.update(provenance)  # source_repo / source_path / minio_object_key / source_version
    config_str = json.dumps(cfg, ensure_ascii=False)
    tags_str = json.dumps(parsed.get("tags") or [], ensure_ascii=False)
    row = s.exec(
        select(CapabilityItem).where(
            CapabilityItem.type == ASSET_EXPERT_TEMPLATE, CapabilityItem.name == name
        )
    ).first()
    if row is None:
        s.add(CapabilityItem(
            type=ASSET_EXPERT_TEMPLATE, name=name,
            description=parsed.get("description", ""), tags=tags_str, config=config_str,
        ))
        created = True
    else:
        row.description = parsed.get("description", "") or row.description
        row.tags = tags_str
        row.config = config_str
        row.updated_at = _utcnow()
        s.add(row)
        created = False
    s.commit()
    return created


# ── Orchestration (D1) ───────────────────────────────────────────────────────

def run_ingestion(*, source_name: str = DEFAULT_SOURCE_NAME,
                  source_repo: str = DEFAULT_SOURCE_REPO,
                  source_version: str = "",
                  fetcher: Optional[AssetFetcher] = None,
                  session: Optional[Session] = None) -> dict:
    """Run one import batch: fetch → store → parse → classify → index.

    Returns a summary dict. Per-file failures degrade (failed event + skip); the
    batch always finishes and the AssetIngestRun is closed completed/failed."""
    own = session is None
    s = session or Session(engine)
    if fetcher is None:
        fetcher = GiteeFetcher(repo=source_repo)
    version = source_version or _utcnow().strftime("%Y%m%dT%H%M%S")

    run = AssetIngestRun(
        source_name=source_name, source_repo=source_repo, storage=obj.backend_name() if hasattr(obj, "backend_name") else "minio",
        bucket=obj.BUCKET_KNOWLEDGE, prefix=RAW_PREFIX, source_version=version, status="running",
    )
    s.add(run)
    s.commit()
    s.refresh(run)
    run_id = run.id

    counts = {"total": 0, "stored": 0, "indexed": 0, "failed": 0, "skipped_docs": 0}
    manifest_files: List[dict] = []

    try:
        try:
            files = fetcher.list_tree()
        except Exception as exc:
            _record_event(s, run_id=run_id, event_type="fetch", status="failed",
                          message=f"list_tree failed: {exc}")
            files = []
        counts["total"] = len(files)

        for f in files:
            sp = f.source_path
            # 1. fetch
            try:
                md = fetcher.fetch_raw(sp)
                _record_event(s, run_id=run_id, event_type="fetch", source_path=sp)
            except Exception as exc:
                counts["failed"] += 1
                _record_event(s, run_id=run_id, event_type="fetch", status="failed",
                              source_path=sp, message=str(exc))
                continue
            # 2. store raw → object storage
            object_key = f"{RAW_PREFIX}/{sp}"
            try:
                ok = obj.put_object(obj.BUCKET_KNOWLEDGE, object_key,
                                    md.encode("utf-8"), "text/markdown")
                if ok:
                    counts["stored"] += 1
                _record_event(s, run_id=run_id, event_type="store",
                              status="success" if ok else "failed",
                              source_path=sp, object_key=object_key)
            except Exception as exc:
                _record_event(s, run_id=run_id, event_type="store", status="failed",
                              source_path=sp, object_key=object_key, message=str(exc))
            # 3. parse + classify
            try:
                fm = _parse_frontmatter(md)
                asset_type = classify_asset(sp, md, fm)
                _record_event(s, run_id=run_id, event_type="classify",
                              source_path=sp, object_key=object_key, asset_type=asset_type)
            except Exception as exc:
                counts["failed"] += 1
                _record_event(s, run_id=run_id, event_type="parse", status="failed",
                              source_path=sp, object_key=object_key, message=str(exc))
                continue
            manifest_files.append({"source_path": sp, "object_key": object_key,
                                   "asset_type": asset_type})
            # 4. index (expert_template only; docs/others archived but not indexed)
            if asset_type == ASSET_EXPERT_TEMPLATE:
                try:
                    parsed = parse_expert_template(sp, md, fm)
                    provenance = {
                        "source_repo": source_repo, "source_path": sp,
                        "minio_object_key": object_key, "source_version": version,
                    }
                    _index_expert_template(s, parsed, provenance)
                    counts["indexed"] += 1
                    _record_event(s, run_id=run_id, event_type="index",
                                  source_path=sp, object_key=object_key,
                                  asset_type=asset_type, message=parsed["name"])
                except Exception as exc:
                    counts["failed"] += 1
                    _record_event(s, run_id=run_id, event_type="index", status="failed",
                                  source_path=sp, object_key=object_key,
                                  asset_type=asset_type, message=str(exc))
            elif asset_type == ASSET_DOCS:
                counts["skipped_docs"] += 1

        # manifest → object storage
        manifest_key = f"{MANIFEST_PREFIX}/{version}.json"
        manifest = {
            "source_name": source_name, "source_repo": source_repo,
            "source_version": version, "synced_at": _utcnow().isoformat(),
            "counts": counts, "files": manifest_files,
        }
        obj.put_object(obj.BUCKET_KNOWLEDGE, manifest_key,
                       json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
                       "application/json")

        run.manifest_key = manifest_key
        run.status = "completed"
        run.summary_json = json.dumps(counts, ensure_ascii=False)
        run.completed_at = _utcnow()
        s.add(run)
        s.commit()
        return {"run_id": run_id, "status": "completed", "counts": counts,
                "manifest_key": manifest_key}
    except Exception as exc:
        _log.warning("ingestion run failed (%s)", exc)
        try:
            run.status = "failed"
            run.summary_json = json.dumps({**counts, "error": str(exc)}, ensure_ascii=False)
            run.completed_at = _utcnow()
            s.add(run)
            s.commit()
        except Exception:
            s.rollback()
        return {"run_id": run_id, "status": "failed", "counts": counts, "error": str(exc)}
    finally:
        if own:
            s.close()
