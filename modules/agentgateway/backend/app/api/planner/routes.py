"""Planner API router — registers all route handlers defined across the package.

The router prefix/tags and the full route set live here. Handler bodies live in
their semantic modules (sessions / apply / ws); this module imports them and
registers each via add_api_route / add_api_websocket_route so the route table is
identical to the pre-split single-file module.
"""

from typing import List

from fastapi import APIRouter

from .schemas import (
    StartResponse,
    PlannerSessionListItem,
    PlannerSessionDetail,
    FromAgentResponse,
    AttachmentMeta,
    PlannerFileItem,
    PlannerSkillItem,
    PlannerSnapshotResponse,
    PlanningContextResponse,
)
from . import sessions, apply, ws, context

router = APIRouter(prefix="/planner", tags=["planner"])

# ── Session lifecycle / start ──
router.add_api_route("/start", sessions.start_planner, methods=["POST"], response_model=StartResponse)
router.add_api_route("/sessions", sessions.list_planner_sessions, methods=["GET"], response_model=List[PlannerSessionListItem])
router.add_api_route("/sessions/{conversation_id}", sessions.get_planner_session, methods=["GET"], response_model=PlannerSessionDetail)
router.add_api_route("/sessions/{conversation_id}", sessions.delete_planner_session, methods=["DELETE"])

# ── Planning context (read-only aggregation, T1) ──
router.add_api_route("/planning-context", context.get_planning_context, methods=["GET"], response_model=PlanningContextResponse)

# ── Expert template retrieval (read-only, T3) ──
router.add_api_route("/expert-templates/retrieve", context.retrieve_expert_templates, methods=["GET"])

# ── Asset ingestion observability (read-only, T10) ──
router.add_api_route("/asset-ingest/runs", context.list_asset_ingest_runs, methods=["GET"])
router.add_api_route("/asset-ingest/runs/{run_id}/events", context.list_asset_ingest_events, methods=["GET"])

# ── Proposal lifecycle (T5) ──
router.add_api_route("/proposals/{conversation_id}", context.get_proposal_by_conversation, methods=["GET"])
router.add_api_route("/proposals/by-id/{proposal_id}", context.get_proposal_by_id, methods=["GET"])
router.add_api_route("/proposals/by-id/{proposal_id}/transition", context.transition_proposal, methods=["POST"])

# ── Draft Agent compile + test (T6) ──
router.add_api_route("/draft-agents/{draft_agent_id}", context.get_draft_agent, methods=["GET"])
router.add_api_route("/draft-agents/{draft_agent_id}/compile", context.compile_draft_agent_endpoint, methods=["POST"])

# ── Planner run events (read-only, T8) ──
router.add_api_route("/runs/{run_id}/events", context.get_run_events, methods=["GET"])
router.add_api_route("/proposals/by-id/{proposal_id}/events", context.get_run_events_by_proposal, methods=["GET"])

# ── Memory observability (read-only, T9) ──
router.add_api_route("/memory-events/by-run/{run_id}", context.list_memory_events_by_run, methods=["GET"])
router.add_api_route("/memory-events/by-proposal/{proposal_id}", context.list_memory_events_for_proposal, methods=["GET"])
router.add_api_route("/memory-items/audit", context.audit_memory_items, methods=["GET"])

# ── Attachments ──
router.add_api_route("/sessions/{conversation_id}/attachments", sessions.upload_planner_attachment, methods=["POST"], response_model=AttachmentMeta)
router.add_api_route("/sessions/{conversation_id}/attachments/{filename}", sessions.get_planner_attachment, methods=["GET"])

# ── From-agent (replan) / files / skills ──
router.add_api_route("/sessions/from-agent/{agent_id}", sessions.create_session_from_agent, methods=["POST"], response_model=FromAgentResponse)
router.add_api_route("/sessions/{conversation_id}/files", sessions.list_planner_session_files, methods=["GET"], response_model=List[PlannerFileItem])
router.add_api_route("/sessions/{conversation_id}/files/{filename}", sessions.get_planner_session_file, methods=["GET"])
router.add_api_route("/skills", sessions.list_planner_skills, methods=["GET"], response_model=List[PlannerSkillItem])

# ── WebSocket ──
router.add_api_websocket_route("/ws/{conversation_id}", ws.planner_websocket)

# ── Apply / replan / snapshot ──
router.add_api_route("/apply", apply.apply_proposal, methods=["POST"])
router.add_api_route("/apply-replan", apply.apply_replan, methods=["POST"])
router.add_api_route("/proposal/{agent_id}", apply.get_planner_snapshot, methods=["GET"], response_model=PlannerSnapshotResponse)
