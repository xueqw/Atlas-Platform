"""Helpers for self-filling DAG node config from chat.

Shared by the node-config WS path (and any future REST path) to:
- extract a JSON config block out of an assistant reply (D2),
- filter it against the node's ``config_schema`` (D3),
- partial-replace it into the latest DAGGraph + append a ``_chat_history``
  audit row, writing a new version (D4/D5/D6).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select, desc

CHAT_HISTORY_KEY = "_chat_history"
CHAT_HISTORY_MAX = 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def extract_config_block(text: str) -> Optional[dict]:
    """Pull a config dict out of an assistant reply.

    1. Prefer the last ```json fenced block.
    2. Fallback: the first balanced ``{...}`` object found in the full text.
    3. Return None if nothing parses to a dict.
    """
    if not text:
        return None

    blocks = _find_fenced_json_blocks(text)
    for raw in reversed(blocks):
        parsed = _try_load_dict(raw)
        if parsed is not None:
            return parsed

    raw = _first_balanced_object(text)
    if raw is not None:
        return _try_load_dict(raw)

    return None


def _find_fenced_json_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    needle = "```"
    idx = 0
    while True:
        start = text.find(needle, idx)
        if start == -1:
            break
        # Skip the optional language tag on the opening fence line.
        line_end = text.find("\n", start)
        if line_end == -1:
            break
        end = text.find(needle, line_end + 1)
        if end == -1:
            break
        blocks.append(text[line_end + 1 : end])
        idx = end + len(needle)
    return blocks


def _first_balanced_object(text: str) -> Optional[str]:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _try_load_dict(raw: str) -> Optional[dict]:
    try:
        parsed = json.loads(raw.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


_JSON_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}


def validate_against_schema(cfg: dict, schema_str: str) -> tuple[dict, list[str]]:
    """Filter ``cfg`` against a node ``config_schema`` (stringified JSON Schema).

    - Keys not present in ``schema.properties`` are dropped (D3, no error).
    - Recognized keys are kept; if a value's type looks wrong, the key is still
      kept (form-save validates later) but a warning string is collected.
    Returns ``(filtered_cfg, type_warnings)``.
    """
    if not isinstance(cfg, dict):
        return {}, []

    try:
        schema = json.loads(schema_str) if isinstance(schema_str, str) else (schema_str or {})
    except (json.JSONDecodeError, ValueError):
        schema = {}

    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    if not isinstance(properties, dict) or not properties:
        # No usable schema → keep everything, no type checks.
        return dict(cfg), []

    filtered: dict = {}
    warnings: list[str] = []
    for key, value in cfg.items():
        if key not in properties:
            continue
        filtered[key] = value
        expected = properties[key].get("type") if isinstance(properties[key], dict) else None
        check = _JSON_TYPE_CHECKS.get(expected) if expected else None
        if check is not None and not check(value):
            warnings.append(
                f"key '{key}' expected {expected}, got {type(value).__name__}"
            )
    return filtered, warnings


def _get_engine():
    """Resolve the live engine lazily so test conftest patching is honored."""
    from app.core import database
    return database.engine


def merge_into_graph(
    agent_id: int,
    node_id: str,
    partial: dict,
    source: str = "assistant_extracted",
    session: Optional[Session] = None,
) -> tuple[int, list[str]]:
    """Partial-replace ``partial`` into the node's config in the latest DAGGraph.

    Reads the latest DAGGraph for ``agent_id``, top-level-replaces only the keys
    in ``partial`` on the target node's config (D4), appends a ``_chat_history``
    audit row capped at 20 (D6), and writes a new DAGGraph version (D5).
    Returns ``(new_version, changed_keys)``. Raises ValueError if the graph or
    node is missing.
    """
    if session is not None:
        return _merge_with_session(session, agent_id, node_id, partial, source)
    with Session(_get_engine()) as own_session:
        return _merge_with_session(own_session, agent_id, node_id, partial, source)


def _merge_with_session(
    session: Session,
    agent_id: int,
    node_id: str,
    partial: dict,
    source: str,
) -> tuple[int, list[str]]:
    from app.models.db import DAGGraph

    latest = session.exec(
        select(DAGGraph).where(DAGGraph.agent_id == agent_id).order_by(desc(DAGGraph.version))
    ).first()
    if latest is None:
        raise ValueError(f"No DAGGraph found for agent {agent_id}")

    graph = json.loads(latest.graph_json)
    nodes = graph.get("nodes", [])
    node = next((n for n in nodes if n.get("id") == node_id), None)
    if node is None:
        raise ValueError(f"Node {node_id} not found in graph")

    config = dict(node.get("config", {}))
    changed_keys: list[str] = []
    for key, value in partial.items():
        if key == CHAT_HISTORY_KEY or key.startswith("_"):
            continue
        if config.get(key) != value:
            changed_keys.append(key)
        config[key] = value

    history = list(config.get(CHAT_HISTORY_KEY, []))
    turn = (history[-1]["turn"] + 1) if history else 1
    history.append({
        "turn": turn,
        "at": _now_iso(),
        "changed_keys": list(changed_keys),
        "source": source,
    })
    if len(history) > CHAT_HISTORY_MAX:
        history = history[-CHAT_HISTORY_MAX:]
    config[CHAT_HISTORY_KEY] = history

    node["config"] = config

    new_version = (latest.version or 0) + 1
    new_graph = DAGGraph(
        agent_id=agent_id,
        graph_json=json.dumps(graph, ensure_ascii=False),
        state_schema=latest.state_schema,
        version=new_version,
    )
    session.add(new_graph)
    session.commit()
    session.refresh(new_graph)
    return new_graph.version, changed_keys
