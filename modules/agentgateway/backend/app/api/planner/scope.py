"""Planner request scope resolution and row-isolation helpers.

AgentGateway currently runs without a mandatory auth provider in local mode.
These explicit headers form the compatibility boundary until the platform
gateway injects verified claims. Missing headers intentionally resolve to the
well-known ``default`` scope so the existing local frontend keeps working.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Optional

from fastapi import Header, HTTPException, WebSocket
from app.models.db import PlannerSession


DEFAULT_SCOPE_ID = "default"
_SCOPE_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")


def _normalize_scope_id(value: Optional[str]) -> str:
    normalized = str(value or DEFAULT_SCOPE_ID).strip() or DEFAULT_SCOPE_ID
    if not _SCOPE_RE.fullmatch(normalized):
        raise HTTPException(status_code=400, detail="invalid planner scope header")
    return normalized


@dataclass(frozen=True)
class PlannerScope:
    tenant_id: str = DEFAULT_SCOPE_ID
    workspace_id: str = DEFAULT_SCOPE_ID
    user_id: str = DEFAULT_SCOPE_ID

    def as_dict(self) -> dict[str, str]:
        return {
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
        }


DEFAULT_PLANNER_SCOPE = PlannerScope()


def resolve_planner_scope(
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
    x_workspace_id: Optional[str] = Header(None, alias="X-Workspace-ID"),
    x_user_id: Optional[str] = Header(None, alias="X-User-ID"),
) -> PlannerScope:
    return PlannerScope(
        tenant_id=_normalize_scope_id(x_tenant_id),
        workspace_id=_normalize_scope_id(x_workspace_id),
        user_id=_normalize_scope_id(x_user_id),
    )


def scope_from_websocket(websocket: WebSocket) -> PlannerScope:
    # Lightweight test/fallback WebSocket adapters may not expose a headers
    # mapping; that is equivalent to the local frontend omitting scope headers.
    headers = getattr(websocket, "headers", None) or {}
    return PlannerScope(
        tenant_id=_normalize_scope_id(headers.get("x-tenant-id")),
        workspace_id=_normalize_scope_id(headers.get("x-workspace-id")),
        user_id=_normalize_scope_id(headers.get("x-user-id")),
    )


def coerce_planner_scope(value: Any) -> PlannerScope:
    """Keep direct helper/test calls backward compatible with FastAPI Depends."""
    if isinstance(value, PlannerScope):
        return value
    if isinstance(value, dict):
        return PlannerScope(
            tenant_id=_normalize_scope_id(value.get("tenant_id")),
            workspace_id=_normalize_scope_id(value.get("workspace_id")),
            user_id=_normalize_scope_id(value.get("user_id")),
        )
    return DEFAULT_PLANNER_SCOPE


def scope_session_select(statement: Any, scope: PlannerScope) -> Any:
    return statement.where(
        PlannerSession.tenant_id == scope.tenant_id,
        PlannerSession.workspace_id == scope.workspace_id,
        PlannerSession.user_id == scope.user_id,
    )
