"""Plan-with-File delivery layer for the DeerFlow planner.

This module owns the on-disk artifacts that accumulate during a planning
session, so the chat surface only needs to render concise summaries plus
file entrypoints. The contract follows skills/plan-with-file/SKILL.md:

  backend/data/planner-sessions/{conversation_id}/
    requirements.md   — current requirement summary + confirmed constraints
    architecture.md   — most recent architecture rationale (after first draft)
    proposal.json     — final structured proposal (canonical machine artifact)
    decisions.md      — append-only A2UI confirmation log
    final.md          — short summary written before apply

Every writer returns a dict {path, kind, summary, updated_at} that planner.py
echoes back to the websocket as a `plan_file_updated` event so the frontend
PlanFileViewer can refresh its index without an extra round-trip.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _utcnow() -> datetime:
    """Naive UTC datetime drop-in for the deprecated ``datetime.utcnow()``."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utcfromtimestamp(ts: float) -> datetime:
    """Naive UTC drop-in for the deprecated ``datetime.utcfromtimestamp``."""
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)

# All session artifacts live under a single root next to the SQLite DB so
# housekeeping (backup / wipe dev DB / migration) treats them as one unit.
_ROOT = Path(__file__).resolve().parents[2] / "data" / "planner-sessions"


def _session_dir(conversation_id: str) -> Path:
    """Return (and lazily create) the directory for a planner session."""
    if not conversation_id or "/" in conversation_id or ".." in conversation_id:
        raise ValueError(f"invalid conversation_id: {conversation_id!r}")
    d = _ROOT / conversation_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _artifact(conversation_id: str, filename: str, kind: str, summary: str) -> Dict[str, Any]:
    return {
        "path": f"{conversation_id}/{filename}",
        "filename": filename,
        "kind": kind,
        "summary": summary,
        "updated_at": _utcnow().isoformat(timespec="seconds") + "Z",
    }


def write_requirements(
    conversation_id: str,
    memory: Dict[str, Any],
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Capture requirement_summary + confirmed_constraints + task_classification.

    Overwrite each turn — file represents the *current* understanding, not
    the history. The change_log inside this file shows when each field
    last moved. When ``attachments`` is provided, a "用户附件" section lists
    each attachment by kind / relative path / size.
    """
    summary = (memory or {}).get("requirement_summary") or ""
    constraints = (memory or {}).get("confirmed_constraints") or []
    classification = (memory or {}).get("task_classification") or ""
    feedback = (memory or {}).get("user_feedback") or []

    lines: List[str] = ["# 需求摘要", ""]
    if summary:
        lines += [summary, ""]
    else:
        lines += ["（尚未确认）", ""]
    if classification:
        lines += [f"**任务分型**：{classification}", ""]
    if constraints:
        lines += ["## 已确认约束", ""]
        lines += [f"- {c}" for c in constraints]
        lines += [""]
    if attachments:
        lines += ["## 用户附件", ""]
        for a in attachments:
            kind = a.get("kind", "")
            path = a.get("path") or a.get("name") or ""
            size_kb = max(1, round(int(a.get("size") or 0) / 1024))
            lines.append(f"- {kind} {path} ({size_kb} KiB)")
        lines += [""]
    if feedback:
        lines += ["## 用户反馈历史", ""]
        lines += [f"- {f}" for f in feedback]
        lines += [""]
    lines += [
        f"_最近更新：{_utcnow().isoformat(timespec='seconds')}Z_",
    ]
    target = _session_dir(conversation_id) / "requirements.md"
    target.write_text("\n".join(lines), encoding="utf-8")
    return _artifact(conversation_id, "requirements.md", "requirements", summary[:80] or "尚未确认需求")


def write_architecture(conversation_id: str, summary: str, rationale: Optional[str] = None) -> Dict[str, Any]:
    """Persist the latest architecture rationale once a draft proposal exists."""
    lines: List[str] = ["# 架构方案", ""]
    if summary:
        lines += [summary, ""]
    if rationale:
        lines += ["## 设计理由", "", rationale, ""]
    lines += [f"_最近更新：{_utcnow().isoformat(timespec='seconds')}Z_"]
    target = _session_dir(conversation_id) / "architecture.md"
    target.write_text("\n".join(lines), encoding="utf-8")
    return _artifact(conversation_id, "architecture.md", "architecture", summary[:80] or "（架构概要待补充）")


def write_proposal_json(conversation_id: str, proposal: Dict[str, Any]) -> Dict[str, Any]:
    """Write the canonical structured proposal. The chat surface NEVER
    renders this content directly — the frontend opens this file via
    PlanFileViewer instead."""
    target = _session_dir(conversation_id) / "proposal.json"
    target.write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8")
    nodes = proposal.get("nodes") if isinstance(proposal, dict) else None
    n = len(nodes) if isinstance(nodes, list) else 0
    return _artifact(conversation_id, "proposal.json", "proposal", f"结构化方案：{n} 节点")


def append_decision(
    conversation_id: str,
    prompt: str,
    choice: str,
    free_text: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Append-only log of A2UI confirmations. Each record is one user pick."""
    target = _session_dir(conversation_id) / "decisions.md"
    ts = _utcnow().isoformat(timespec="seconds") + "Z"
    block: List[str] = []
    if not target.exists():
        block += ["# 人类确认记录", ""]
    block += [f"## {ts}"]
    if request_id:
        block += [f"- request_id: `{request_id}`"]
    block += [f"- prompt: {prompt}", f"- 选择: **{choice}**"]
    if free_text:
        block += [f"- 备注: {free_text}"]
    block += [""]
    with target.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(block) + "\n")
    return _artifact(conversation_id, "decisions.md", "decisions", f"记录决策：{choice}")


def write_final_summary(
    conversation_id: str,
    summary_text: str,
    files: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Short human-facing summary written before apply."""
    lines: List[str] = ["# 最终交付", "", summary_text or "（无摘要）", ""]
    if files:
        lines += ["## 完整产物", ""]
        for f in files:
            kind = f.get("kind", "")
            path = f.get("path") or f.get("filename") or ""
            note = f.get("summary") or ""
            lines.append(f"- **{kind}** — `{path}`{f' — {note}' if note else ''}")
        lines.append("")
    lines += [f"_生成时间：{_utcnow().isoformat(timespec='seconds')}Z_"]
    target = _session_dir(conversation_id) / "final.md"
    target.write_text("\n".join(lines), encoding="utf-8")
    return _artifact(conversation_id, "final.md", "final", (summary_text or "")[:80] or "（无摘要）")


def list_artifacts(conversation_id: str) -> List[Dict[str, Any]]:
    """Enumerate files currently on disk for this session."""
    d = _ROOT / conversation_id
    if not d.exists():
        return []
    out: List[Dict[str, Any]] = []
    for entry in sorted(d.iterdir()):
        if entry.is_file():
            stat = entry.stat()
            out.append({
                "path": f"{conversation_id}/{entry.name}",
                "filename": entry.name,
                "kind": entry.stem,
                "size": stat.st_size,
                "updated_at": _utcfromtimestamp(stat.st_mtime).isoformat(timespec="seconds") + "Z",
            })
    return out


def read_artifact(conversation_id: str, filename: str) -> Optional[str]:
    """Read a single artifact by filename. Rejects path traversal."""
    if not filename or "/" in filename or ".." in filename:
        raise ValueError(f"invalid filename: {filename!r}")
    if not conversation_id or "/" in conversation_id or ".." in conversation_id:
        raise ValueError(f"invalid conversation_id: {conversation_id!r}")
    target = _ROOT / conversation_id / filename
    if not target.exists() or not target.is_file():
        return None
    return target.read_text(encoding="utf-8")


def merge_artifacts(existing: List[Dict[str, Any]], new_one: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Replace prior entry with the same filename, append otherwise."""
    fname = new_one.get("filename") or new_one.get("path")
    out = [a for a in (existing or []) if (a.get("filename") or a.get("path")) != fname]
    out.append(new_one)
    return out
