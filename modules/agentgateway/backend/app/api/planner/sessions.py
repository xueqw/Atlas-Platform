"""Planner session persistence, derivation, and session/attachment/file/skill handlers."""

import json
import shutil
import uuid
from typing import Dict, List, Optional
from fastapi import HTTPException, UploadFile, File, Query
from fastapi.responses import FileResponse, Response
from sqlmodel import Session, select, desc
from app.core.database import engine
from app.core import planner_files, planner_attachments
from app.core.model_caps import DEFAULT_CHAT_MODEL_ID, DEFAULT_CHAT_PROVIDER
from app.models.db import (
    Agent,
    DAGGraph,
    ArchitectureProposal,
    PlannerSession,
    CapabilityItem,
    _utcnow,
)
from . import state
from .state import (
    PLANNER_GREETING,
    _new_memory,
    _default_source,
    _is_default_skill,
    _normalize_pending_a2ui,
)
from .proposal import _parse_json_list
from .prompts import (
    _resolve_model,
    _build_replan_context,
    _load_trace_summary,
    _load_eval_summary,
    _parse_skill_config,
    resolve_effective_skills,
)
from .schemas import (
    StartRequest,
    StartResponse,
    PlannerSessionListItem,
    PlannerSessionDetail,
    FromAgentResponse,
    AttachmentMeta,
    PlannerFileItem,
    PlannerSkillItem,
)


def _derive_session_title(memory: dict, messages: List[dict]) -> str:
    """Derive a human-friendly session title from memory or first user message."""
    summary = (memory or {}).get("requirement_summary") or ""
    if summary:
        return summary[:80]
    for m in messages or []:
        if m.get("role") == "user":
            return str(m.get("content", ""))[:80]
    return "未命名会话"


def _infer_stage(memory: dict, proposal: Optional[dict], applied: bool) -> str:
    """Infer the planner stage from current state."""
    if applied:
        return "applied"
    if proposal:
        return "ready_to_apply"
    if memory and memory.get("latest_proposal_summary"):
        return "awaiting_confirmation"
    if memory and (memory.get("requirement_summary") or memory.get("confirmed_constraints")):
        return "drafting"
    return "clarifying"


def _serialize_planning_state(memory: dict) -> str:
    """Serialize the 14 richer-state fields into planning_state_json (design D1).

    decisions_confirmed / architecture_pattern / apply_readiness are ALSO stored
    in their own columns, but kept here too so the blob is a complete snapshot
    (single source on load). Legacy 6 fields stay in their existing columns."""
    keys = (
        "open_questions", "assumptions", "non_goals", "success_metrics",
        "decisions_pending", "decisions_confirmed", "tradeoffs", "risks",
        "architecture_pattern", "runtime_strategy", "memory_strategy",
        "knowledge_strategy", "evaluation_strategy", "apply_readiness",
        # Plan-first structured proposal mirror (T2). Persisted so the validated
        # ProposalPayload round-trips inside planner_sessions without a new table.
        "proposal_payload",
    )
    state = {k: memory.get(k) for k in keys if memory.get(k) not in (None, "", [], {})}
    return json.dumps(state, ensure_ascii=False)


def _persist_session(conv_id: str, conv_data: dict) -> None:
    """Upsert PlannerSession row from in-memory conv_data. Called after every turn."""
    memory = conv_data.get("memory") or _new_memory()
    messages = conv_data.get("messages") or []
    proposal = conv_data.get("proposal")
    linked_agent_id = conv_data.get("linked_agent_id")
    title = conv_data.get("session_title") or _derive_session_title(memory, messages)
    stage = _infer_stage(memory, proposal, linked_agent_id is not None)
    mode = conv_data.get("mode") or "create"
    replan_context = conv_data.get("replan_context") or ""

    # Richer-state serialization (design D1): aggregate blob + 3 query columns.
    planning_state_json = _serialize_planning_state(memory)
    decision_log_json = json.dumps(memory.get("decisions_confirmed", []) or [], ensure_ascii=False)
    architecture_pattern = str(memory.get("architecture_pattern", "") or "")[:50]
    apply_readiness_json = json.dumps(memory.get("apply_readiness", {}) or {}, ensure_ascii=False)

    with Session(engine) as session:
        row = session.exec(
            select(PlannerSession).where(PlannerSession.conversation_id == conv_id)
        ).first()
        if row is None:
            row = PlannerSession(
                conversation_id=conv_id,
                session_title=title,
                stage=stage,
                user_request=(messages[0]["content"] if messages else "")[:2000],
                requirement_summary=memory.get("requirement_summary", "") or "",
                confirmed_constraints=json.dumps(memory.get("confirmed_constraints", []) or [], ensure_ascii=False),
                task_classification=memory.get("task_classification", "") or "",
                latest_proposal_summary=memory.get("latest_proposal_summary", "") or "",
                planner_messages=json.dumps(messages, ensure_ascii=False),
                file_artifacts=json.dumps(conv_data.get("file_artifacts", []) or [], ensure_ascii=False),
                selected_skills=json.dumps(memory.get("selected_skills", []) or [], ensure_ascii=False),
                mode=mode,
                replan_context=replan_context,
                planning_state_json=planning_state_json,
                decision_log_json=decision_log_json,
                architecture_pattern=architecture_pattern,
                apply_readiness_json=apply_readiness_json,
                linked_agent_id=linked_agent_id,
            )
            session.add(row)
        else:
            row.session_title = title
            row.stage = stage
            row.requirement_summary = memory.get("requirement_summary", "") or ""
            row.confirmed_constraints = json.dumps(memory.get("confirmed_constraints", []) or [], ensure_ascii=False)
            row.task_classification = memory.get("task_classification", "") or ""
            row.latest_proposal_summary = memory.get("latest_proposal_summary", "") or ""
            row.planner_messages = json.dumps(messages, ensure_ascii=False)
            row.file_artifacts = json.dumps(conv_data.get("file_artifacts", []) or [], ensure_ascii=False)
            row.selected_skills = json.dumps(memory.get("selected_skills", []) or [], ensure_ascii=False)
            row.mode = mode
            row.replan_context = replan_context
            row.planning_state_json = planning_state_json
            row.decision_log_json = decision_log_json
            row.architecture_pattern = architecture_pattern
            row.apply_readiness_json = apply_readiness_json
            if linked_agent_id is not None:
                row.linked_agent_id = linked_agent_id
            row.last_updated_at = _utcnow()
            session.add(row)
        session.commit()


def _memory_from_row(row: PlannerSession) -> dict:
    """Reconstruct richer planner state from a PlannerSession row.

    Starts from ``_new_memory()`` safe defaults, overlays the legacy 6 columns,
    then overlays the richer-state blob (planning_state_json). Old rows whose
    new columns hold defaults ("{}" / "[]") simply keep the safe defaults — no
    deserialization failure (spec: 旧 session 行兼容加载)."""
    memory = _new_memory()
    memory["requirement_summary"] = row.requirement_summary or ""
    memory["confirmed_constraints"] = _parse_json_list(row.confirmed_constraints)
    memory["task_classification"] = row.task_classification or ""
    memory["latest_proposal_summary"] = row.latest_proposal_summary or ""
    # selected_skills is the user_selected tier ONLY. Strip any default names
    # that may have leaked into the persisted list — defaults are re-derived by
    # resolve_effective_skills() on every prompt build, never trusted from the
    # stored cache (design D4). This keeps restore/replan effective sets stable
    # across changes to the backend default constants.
    persisted_skills = _parse_json_list(getattr(row, "selected_skills", "[]") or "[]")
    memory["selected_skills"] = [s for s in persisted_skills if not _is_default_skill(s)]

    # Richer-state blob (the 14 new fields). Tolerant: malformed JSON → defaults.
    try:
        state = json.loads(getattr(row, "planning_state_json", "{}") or "{}")
        if isinstance(state, dict):
            for k, v in state.items():
                if k in memory and v not in (None, "", [], {}):
                    memory[k] = v
    except (json.JSONDecodeError, TypeError):
        pass

    # Query-friendly columns are authoritative when present (they're written on
    # every persist alongside the blob).
    try:
        ledger = json.loads(getattr(row, "decision_log_json", "[]") or "[]")
        if isinstance(ledger, list) and ledger:
            memory["decisions_confirmed"] = ledger
    except (json.JSONDecodeError, TypeError):
        pass
    pattern = getattr(row, "architecture_pattern", "") or ""
    if pattern:
        memory["architecture_pattern"] = pattern
    try:
        readiness = json.loads(getattr(row, "apply_readiness_json", "{}") or "{}")
        if isinstance(readiness, dict) and readiness:
            memory["apply_readiness"] = readiness
    except (json.JSONDecodeError, TypeError):
        pass
    return memory


def _pending_a2ui_from_memory(memory: dict) -> Optional[dict]:
    """Rebuild a pending A2UI card from persisted decisions_pending.

    Legacy single-call planner turns can persist the decision in memory while the
    transient WS ``a2ui_request`` event is missed by the browser. On restore we
    derive a conservative card from the first pending decision so human
    interaction is never stranded behind prose like "已发起确认卡片".
    """
    if not isinstance(memory, dict):
        return None
    readiness = memory.get("apply_readiness") or {}
    if isinstance(readiness, dict) and readiness.get("status") == "ready":
        return None
    pending = memory.get("decisions_pending") or []
    if not isinstance(pending, list):
        return None
    for item in pending:
        if not isinstance(item, dict):
            continue
        raw_options = item.get("options") or []
        if not isinstance(raw_options, list) or not raw_options:
            continue
        options: list[dict] = []
        for idx, opt in enumerate(raw_options, start=1):
            if isinstance(opt, dict):
                label = str(opt.get("label") or opt.get("id") or "").strip()
                opt_id = str(opt.get("id") or f"option_{idx}").strip()
                desc = opt.get("description")
            else:
                label = str(opt).strip()
                opt_id = f"option_{idx}"
                desc = None
            if not label:
                continue
            out = {"id": opt_id, "label": label}
            if desc:
                out["description"] = str(desc)
            options.append(out)
        if not options:
            continue
        topic = str(item.get("topic") or "待确认事项").strip()
        prompt = str(item.get("prompt") or f"请选择：{topic}").strip()
        return _normalize_pending_a2ui({
            "id": str(item.get("id") or "").strip(),
            "topic": topic,
            "prompt": prompt,
            "options": options,
        })
    return None


def _load_session_into_memory(conv_id: str) -> Optional[dict]:
    """Load PlannerSession row into the in-memory conv_data shape. Returns None if not found."""
    with Session(engine) as session:
        row = session.exec(
            select(PlannerSession).where(PlannerSession.conversation_id == conv_id)
        ).first()
        if row is None:
            return None
        memory = _memory_from_row(row)
        try:
            messages = json.loads(row.planner_messages or "[]")
            messages = messages if isinstance(messages, list) else []
        except (json.JSONDecodeError, TypeError):
            messages = []
        try:
            artifacts = json.loads(row.file_artifacts or "[]")
            artifacts = artifacts if isinstance(artifacts, list) else []
        except (json.JSONDecodeError, TypeError):
            artifacts = []
        conv_data = {
            "messages": messages,
            "memory": memory,
            "model": {"model_name": DEFAULT_CHAT_MODEL_ID, "provider": DEFAULT_CHAT_PROVIDER},
            "session_title": row.session_title or "",
            "stage": row.stage or "clarifying",
            "linked_agent_id": row.linked_agent_id,
            "file_artifacts": artifacts,
            "mode": getattr(row, "mode", "create") or "create",
            "replan_context": getattr(row, "replan_context", "") or "",
        }
        pending_a2ui = _pending_a2ui_from_memory(memory)
        if pending_a2ui:
            conv_data["pending_a2ui"] = pending_a2ui
        return conv_data


def start_planner(body: StartRequest = StartRequest()):
    conv_id = str(uuid.uuid4())
    resolved = _resolve_model(body.model_id)
    conv_data = {
        "messages": [],
        "model": resolved,
        "memory": _new_memory(),
        "file_artifacts": [],
    }
    state.store_conv(conv_id, conv_data)
    # Persist a stub row immediately so a refresh before the first turn still
    # finds the session (and the sidebar lists it). Best-effort — DB hiccups
    # must not break the start handshake.
    try:
        _persist_session(conv_id, conv_data)
    except Exception:
        pass
    return StartResponse(conversation_id=conv_id, greeting=PLANNER_GREETING, resolved_model=resolved["model_name"])


def list_planner_sessions(limit: int = 50):
    with Session(engine) as session:
        rows = session.exec(
            select(PlannerSession).order_by(desc(PlannerSession.last_updated_at)).limit(limit)
        ).all()
        return [
            PlannerSessionListItem(
                conversation_id=r.conversation_id,
                session_title=r.session_title or "未命名会话",
                stage=r.stage or "clarifying",
                linked_agent_id=r.linked_agent_id,
                last_updated_at=r.last_updated_at,
                created_at=r.created_at,
            )
            for r in rows
        ]


def get_planner_session(conversation_id: str):
    with Session(engine) as session:
        row = session.exec(
            select(PlannerSession).where(PlannerSession.conversation_id == conversation_id)
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="planner session not found")
        memory = _memory_from_row(row)
        try:
            messages = json.loads(row.planner_messages or "[]")
            messages = messages if isinstance(messages, list) else []
        except (json.JSONDecodeError, TypeError):
            messages = []
        try:
            artifacts = json.loads(row.file_artifacts or "[]")
            artifacts = artifacts if isinstance(artifacts, list) else []
        except (json.JSONDecodeError, TypeError):
            artifacts = []
        return PlannerSessionDetail(
            conversation_id=row.conversation_id,
            session_title=row.session_title or "未命名会话",
            stage=row.stage or "clarifying",
            user_request=row.user_request or "",
            memory=memory,
            messages=messages,
            file_artifacts=artifacts,
            linked_agent_id=row.linked_agent_id,
            mode=getattr(row, "mode", "create") or "create",
            replan_context=getattr(row, "replan_context", "") or "",
            last_updated_at=row.last_updated_at,
            created_at=row.created_at,
        )


async def delete_planner_session(conversation_id: str):
    """Delete a planner session: in-memory state, live socket, plan files, DB row.

    Order matters (design D4): drop the in-memory conv_data and close any live
    WebSocket *before* removing files so a mid-flight turn can't re-create the
    directory or re-persist the row we just deleted. rmtree is best-effort.
    """
    with Session(engine) as session:
        row = session.exec(
            select(PlannerSession).where(PlannerSession.conversation_id == conversation_id)
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="session not found")

    # 1. Drop in-memory conversation state.
    state.drop_conv(conversation_id)

    # 2. Close any active WebSocket for this cid so it stops streaming into a
    #    conversation we're about to delete.
    ws = state._active_websockets.pop(conversation_id, None)
    if ws is not None:
        try:
            await ws.close()
        except Exception:
            pass

    # 3. Remove the on-disk session directory (plan files) and attachment
    #    objects. plan files still live on disk under planner_files._ROOT;
    #    attachments now go through object_storage (MinIO objects or the disk
    #    object-store), so sweep them via the attachments helper too.
    try:
        session_dir = planner_files._ROOT / conversation_id
        shutil.rmtree(session_dir, ignore_errors=True)
    except Exception:
        pass
    try:
        planner_attachments.delete_session_attachments(conversation_id)
    except Exception:
        pass

    # 4. Delete the DB row.
    with Session(engine) as session:
        row = session.exec(
            select(PlannerSession).where(PlannerSession.conversation_id == conversation_id)
        ).first()
        if row is not None:
            session.delete(row)
            session.commit()

    return {"ok": True, "removed": conversation_id}


async def upload_planner_attachment(conversation_id: str, file: UploadFile = File(...)):
    """Accept a single multipart attachment, validate, persist, return metadata."""
    if "/" in conversation_id or ".." in conversation_id:
        raise HTTPException(status_code=400, detail="invalid conversation_id")

    mime = (file.content_type or "").split(";")[0].strip().lower()
    if mime not in planner_attachments.ALLOWED_MIMES:
        raise HTTPException(
            status_code=415,
            detail={"error": "unsupported_media_type", "mime": mime},
        )

    data = await file.read()
    if len(data) > planner_attachments.MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"error": "file_too_large", "limit": planner_attachments.MAX_FILE_BYTES},
        )

    try:
        meta = planner_attachments.save_attachment(conversation_id, file.filename or "file", data, mime)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return AttachmentMeta(
        path=meta["path"],
        kind=meta["kind"],
        mime=meta["mime"],
        name=meta["name"],
        size=meta["size"],
        preview_url=meta["preview_url"],
    )


def get_planner_attachment(conversation_id: str, filename: str):
    """Serve a stored attachment's bytes (used for image previews).

    Reads through object_storage so it works on both the disk and MinIO
    backends; with MinIO a presigned URL could be used directly by the client,
    but proxying keeps the existing same-origin ``preview_url`` contract.
    """
    try:
        data = planner_attachments.read_attachment_bytes(conversation_id, filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if data is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    import mimetypes

    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return Response(content=data, media_type=media_type)


def create_session_from_agent(agent_id: int):
    """Open (or resume) a Replan session for an existing agent.

    If a planner session is already linked to this agent (``linked_agent_id``)
    — either the original planning conversation that produced it, or a prior
    Replan session — we resume that conversation instead of spawning a new one,
    so the user lands back in their existing history. Its Replan context /
    baseline graph are refreshed to the agent's current state and the session is
    switched to ``mode=replan``. Only when no linked session exists do we create
    a fresh one.

    Beyond the historical proposal's memory fields, this loads the agent's
    latest DAGGraph (graph_json + node configs) and, when available, recent
    trace/eval evidence, then assembles a Replan context block that gets
    injected into the system prompt (design D1). Missing optional evidence is
    silently omitted (D4)."""
    with Session(engine) as session:
        agent = session.get(Agent, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")

        applied = session.exec(
            select(ArchitectureProposal)
            .where(ArchitectureProposal.agent_id == agent_id, ArchitectureProposal.status == "applied")
            .order_by(desc(ArchitectureProposal.created_at))
        ).first()
        latest = applied or session.exec(
            select(ArchitectureProposal)
            .where(ArchitectureProposal.agent_id == agent_id)
            .order_by(desc(ArchitectureProposal.created_at))
        ).first()
        if latest is None:
            raise HTTPException(status_code=404, detail="no proposal for this agent")

        memory = _new_memory()
        memory["requirement_summary"] = latest.requirement_summary or ""
        memory["confirmed_constraints"] = _parse_json_list(latest.confirmed_constraints)
        memory["task_classification"] = latest.task_classification or ""
        memory["latest_proposal_summary"] = latest.rationale or latest.requirement_summary or ""
        memory["user_feedback"] = _parse_json_list(latest.user_feedback_summary)
        title = (agent.name or "")[:80] or _derive_session_title(memory, [])

        # Resume an already-linked session if one exists (most recently active
        # first). This is the path that keeps the user's planning history. Look
        # it up BEFORE building the replan context so its richer state (decision
        # ledger, risks, strategies) is restored into memory and injected into
        # the context — replan should not forget prior decisions (task 6.4).
        existing = session.exec(
            select(PlannerSession)
            .where(PlannerSession.linked_agent_id == agent_id)
            .order_by(desc(PlannerSession.last_updated_at))
        ).first()
        if existing is not None:
            prior = _memory_from_row(existing)
            # Overlay accumulated richer state from the prior session onto the
            # proposal-derived base (keep proposal's requirement/constraints as
            # the authoritative baseline, restore the ledger and analysis state).
            for k in (
                "decisions_confirmed", "decisions_pending", "open_questions",
                "assumptions", "non_goals", "success_metrics", "tradeoffs",
                "risks", "architecture_pattern", "runtime_strategy",
                "memory_strategy", "knowledge_strategy", "evaluation_strategy",
                "apply_readiness", "user_feedback",
                # carry user_selected skills forward so replan doesn't degrade to
                # the create minimal set; defaults are re-added by the resolver.
                "selected_skills",
            ):
                if prior.get(k) not in (None, "", [], {}):
                    memory[k] = prior[k]

        # Load the agent's latest DAG graph (the Replan baseline). Fall back to
        # the proposal's proposed_graph_json if no DAGGraph row exists.
        latest_dag = session.exec(
            select(DAGGraph).where(DAGGraph.agent_id == agent_id).order_by(desc(DAGGraph.version))
        ).first()
        graph_json = latest_dag.graph_json if latest_dag else (latest.proposed_graph_json or "{}")

        # Optional evidence (silently omitted when absent).
        trace_summary = _load_trace_summary(session, agent_id)
        eval_summary = _load_eval_summary(session, agent_id)

        replan_context = _build_replan_context(
            agent, graph_json, memory, trace_summary, eval_summary
        )

        # Richer-state columns for whichever row we write.
        planning_state_json = _serialize_planning_state(memory)
        decision_log_json = json.dumps(memory.get("decisions_confirmed", []) or [], ensure_ascii=False)
        architecture_pattern = str(memory.get("architecture_pattern", "") or "")[:50]
        apply_readiness_json = json.dumps(memory.get("apply_readiness", {}) or {}, ensure_ascii=False)

        if existing is not None:
            existing.session_title = title
            existing.requirement_summary = memory["requirement_summary"]
            existing.confirmed_constraints = json.dumps(memory["confirmed_constraints"], ensure_ascii=False)
            existing.task_classification = memory["task_classification"]
            existing.latest_proposal_summary = memory["latest_proposal_summary"]
            existing.mode = "replan"
            existing.replan_context = replan_context
            existing.planning_state_json = planning_state_json
            existing.decision_log_json = decision_log_json
            existing.architecture_pattern = architecture_pattern
            existing.apply_readiness_json = apply_readiness_json
            existing.last_updated_at = _utcnow()
            session.add(existing)
            session.commit()
            return FromAgentResponse(
                conversation_id=existing.conversation_id,
                session_title=title,
                mode="replan",
            )

        new_conv_id = str(uuid.uuid4())
        row = PlannerSession(
            conversation_id=new_conv_id,
            session_title=title,
            stage="clarifying",
            user_request=latest.user_request or "",
            requirement_summary=memory["requirement_summary"],
            confirmed_constraints=json.dumps(memory["confirmed_constraints"], ensure_ascii=False),
            task_classification=memory["task_classification"],
            latest_proposal_summary=memory["latest_proposal_summary"],
            planner_messages="[]",
            file_artifacts="[]",
            mode="replan",
            replan_context=replan_context,
            planning_state_json=planning_state_json,
            decision_log_json=decision_log_json,
            architecture_pattern=architecture_pattern,
            apply_readiness_json=apply_readiness_json,
            linked_agent_id=agent_id,
        )
        session.add(row)
        session.commit()

        return FromAgentResponse(conversation_id=new_conv_id, session_title=title, mode="replan")


def list_planner_session_files(conversation_id: str):
    """List the plan-with-file artifacts the planner has written so far."""
    return [PlannerFileItem(**a) for a in planner_files.list_artifacts(conversation_id)]


def get_planner_session_file(conversation_id: str, filename: str):
    """Read one artifact. Markdown / JSON returned as text in a JSON wrapper so
    the frontend can render either raw or pretty-printed without sniffing
    Content-Type."""
    try:
        content = planner_files.read_artifact(conversation_id, filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if content is None:
        raise HTTPException(status_code=404, detail="file not found")
    return {"filename": filename, "content": content}


def list_planner_skills(conversation_id: Optional[str] = Query(None)):
    """List skill-type capabilities the planner can mount.

    When ``conversation_id`` is provided, each item carries ``attached_to_current``
    plus the three-tier contract fields (default_source / effective_in_current /
    lock_reason) derived from ``resolve_effective_skills()`` so the frontend shows
    the REAL effective set rather than treating selected as effective."""
    selected: set = set()
    if conversation_id:
        with Session(engine) as session:
            row = session.exec(
                select(PlannerSession).where(PlannerSession.conversation_id == conversation_id)
            ).first()
            if row is not None:
                selected = set(_parse_json_list(getattr(row, "selected_skills", "[]") or "[]"))

    # Resolve once: the effective set for this session (defaults always present).
    resolved = resolve_effective_skills(sorted(selected))
    effective = set(resolved["effective_skills"])

    with Session(engine) as session:
        rows = session.exec(
            select(CapabilityItem)
            .where(CapabilityItem.type == "skill")
            .order_by(desc(CapabilityItem.updated_at))
        ).all()

    items: List[PlannerSkillItem] = []
    for r in rows:
        cfg = _parse_skill_config(r.config)
        source = _default_source(r.name)  # "system" | "workspace" | "none"
        is_default = source != "none"
        lock_reason = None
        if is_default:
            lock_reason = (
                "系统默认能力，始终启用，无法卸载"
                if source == "system"
                else "工作区默认能力，始终启用，无法卸载"
            )
        items.append(PlannerSkillItem(
            name=r.name,
            description=r.description or "",
            tags=_parse_json_list(r.tags),
            source=str(cfg.get("source") or "project"),
            entrypoint=str(cfg.get("entrypoint") or ""),
            attached_to_current=(r.name in selected) if conversation_id else None,
            is_default=is_default,
            default_source=source,
            # Defaults are always effective; user skills are effective when selected.
            effective_in_current=(r.name in effective) if conversation_id else None,
            lock_reason=lock_reason,
        ))
    return items
