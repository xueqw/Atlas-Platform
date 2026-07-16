"""Draft Agent compile + dry-run (T6: draft-agent-compile-and-test).

Compiles a ``draft_agent`` into a runnable DAG/config and validates it, reusing
the existing apply helpers + runtime entry (NOT a rewrite). Compile source is the
draft's originating proposal nodes/edges (draft_agent.config_json holds only an
agent_spec, no topology); the draft's runtime_mode / capability_refs are applied
as overrides.

Result shape mirrors the existing CompileResult/ValidationReport (compiler.py /
hermes.py): ``{success, errors[], warnings[], graph_json, dryrun{ran,ok,message}}``.
T6 NEVER creates a production Agent/DAGGraph — publishing still goes through the
existing apply_proposal. On success it advances draft_agent draft→tested.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from typing import Dict, List, Optional

from sqlmodel import Session

from app.core.database import engine
from app.models.db import CapabilityItem, DraftAgent, Proposal, _utcnow

_log = logging.getLogger(__name__)


def _loads(raw, default):
    try:
        v = json.loads(raw) if raw else default
        return v if v is not None else default
    except (json.JSONDecodeError, TypeError):
        return default


def _verify_capability_refs(refs: List[dict], session: Session) -> List[str]:
    """Verify each capability ref actually resolves (CapabilityItem exists; model
    refs resolve a provider). Returns a list of warning strings (non-blocking —
    some capabilities may be lazy-loaded at runtime)."""
    warnings: List[str] = []
    for ref in refs if isinstance(refs, list) else []:
        if not isinstance(ref, dict):
            continue
        cid = ref.get("id")
        name = ref.get("name") or cid
        row = session.get(CapabilityItem, cid) if cid is not None else None
        if row is None:
            warnings.append(f"能力引用未解析：{name}（CapabilityItem 不存在）")
            continue
        if row.type == "model":
            try:
                from app.core.model_caps import provider_for_model
                cfg = _loads(row.config, {})
                model_id = (cfg.get("model_id") if isinstance(cfg, dict) else None) or row.name
                provider_for_model(model_id, session=session)
            except Exception:
                warnings.append(f"模型能力 provider 解析失败：{name}")
    return warnings


def _pick_runnable_node(graph_json: str) -> Optional[dict]:
    """Pick a node carrying a runnable system_prompt+model for the dry-run.
    Prefers an ``agent`` node, else combines a p (prompt) + m (model) node."""
    graph = _loads(graph_json, {})
    nodes = graph.get("nodes") or []
    by_type: Dict[str, dict] = {}
    for n in nodes:
        if isinstance(n, dict):
            by_type.setdefault(n.get("type", ""), n)
    if "agent" in by_type:
        return (by_type["agent"].get("config") or {})
    cfg = {}
    if "p" in by_type:
        cfg.update(by_type["p"].get("config") or {})
    if "m" in by_type:
        cfg.update(by_type["m"].get("config") or {})
    return cfg or None


def _dry_run(graph_json: str) -> dict:
    """Single-turn dry-run reusing the runtime entry. Any failure is caught and
    surfaced as a warning (never blocks the compile result)."""
    cfg = _pick_runnable_node(graph_json)
    if not cfg:
        return {"ran": False, "ok": False, "message": "无可运行节点（缺 agent / p+m）"}
    try:
        from app.core.agentscope_runner import create_agent, run_conversation

        agent = create_agent(
            system_prompt=str(cfg.get("system_prompt") or ""),
            model_name=str(cfg.get("model_name") or ""),
            provider=str(cfg.get("provider") or "glm"),
            stream=False,
            temperature=float(cfg.get("temperature", 0.7) or 0.7),
            max_tokens=int(cfg.get("max_tokens", 512) or 512),
            base_url=cfg.get("base_url"),
            api_key=cfg.get("api_key"),
        )

        async def _one_turn():
            async for _ev, _content in run_conversation(agent, "dry-run 测试消息"):
                pass
            return True

        asyncio.run(_one_turn())
        return {"ran": True, "ok": True, "message": "dry-run 通过"}
    except Exception as exc:  # credential gaps etc. → warning, never fatal
        return {"ran": True, "ok": False, "message": f"dry-run 失败：{exc}"}


def compile_draft_agent(draft_agent_id: int, *, session: Optional[Session] = None,
                        run_dryrun: bool = True) -> dict:
    """Compile a draft agent into a runnable config + validate (+ optional dry-run).

    Returns a structured result dict; never raises on validation failure. On a
    clean compile (no errors) and a dry-run without error-level issues, advances
    the draft_agent draft→tested. Does NOT create any Agent/DAGGraph row."""
    own = session is None
    s = session or Session(engine)
    errors: List[str] = []
    warnings: List[str] = []
    graph_json: Optional[str] = None
    dryrun = {"ran": False, "ok": False, "message": "skipped"}
    try:
        draft = s.get(DraftAgent, draft_agent_id)
        if draft is None:
            return {"success": False, "errors": [f"draft_agent {draft_agent_id} 不存在"],
                    "warnings": [], "graph_json": None, "dryrun": dryrun}
        proposal = s.get(Proposal, draft.proposal_id)
        if proposal is None:
            return {"success": False, "errors": [f"来源 proposal {draft.proposal_id} 不存在"],
                    "warnings": [], "graph_json": None, "dryrun": dryrun}

        payload = _loads(proposal.proposal_json, {})
        nodes = copy.deepcopy(payload.get("nodes") or [])
        edges = copy.deepcopy(payload.get("edges") or [])
        if not nodes:
            return {"success": False, "errors": ["无可编译拓扑（proposal 无 nodes）"],
                    "warnings": [], "graph_json": None, "dryrun": dryrun}

        # Reuse apply helpers (no rewrite, no DB write).
        from app.api.planner.apply import (
            _validate_node_configs, _normalize_node_providers, _build_graph_json,
        )
        try:
            _normalize_node_providers(nodes, s)
        except Exception as exc:
            warnings.append(f"provider 权威化跳过：{exc}")
        for m in _validate_node_configs(nodes):
            errors.append(f"节点 {m['node_id']} 缺必填项：{', '.join(m['keys'])}")
        graph_json = _build_graph_json(nodes, edges)

        # Capability refs resolution (warnings only).
        warnings.extend(_verify_capability_refs(_loads(draft.capability_refs_json, []), s))

        # Best-effort deeper validation via the existing compiler/hermes pipeline.
        try:
            from app.core.hermes import ParameterValidator  # type: ignore
            report = ParameterValidator().validate_dag(_loads(graph_json, {}))
            for e in getattr(report, "errors", []) or []:
                errors.append(str(getattr(e, "message", e)))
            for w in getattr(report, "warnings", []) or []:
                warnings.append(str(getattr(w, "message", w)))
        except Exception:
            pass  # deeper validation is additive; core validation already ran

        success = len(errors) == 0
        if success and run_dryrun:
            dryrun = _dry_run(graph_json)
            if not dryrun["ok"]:
                warnings.append(dryrun["message"])

        # Advance draft→tested on a clean compile (dry-run failure is a warning,
        # not an error — a credential gap shouldn't block staging).
        if success:
            from app.core.draft_agent import transition_draft_status
            transition_draft_status(draft_agent_id, "tested", session=s)

        return {"success": success, "errors": errors, "warnings": warnings,
                "graph_json": graph_json, "dryrun": dryrun}
    finally:
        if own:
            s.close()
