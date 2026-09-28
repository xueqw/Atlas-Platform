"""GitHub connector backed by GitHub's remote MCP server."""
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal
from ..models import ConnectorToken

PROVIDER = "github"
_cache: dict = {"specs": None, "writes": None}


def resolve_pat() -> str:
    """Prefer a PAT saved in the UI, then fall back to the environment."""
    with SessionLocal() as db:
        row = db.scalar(select(ConnectorToken).where(ConnectorToken.provider == PROVIDER))
        if row and row.access_token:
            return row.access_token
    return settings.github_pat


def save_pat(db: Session, pat: str) -> None:
    row = db.scalar(select(ConnectorToken).where(ConnectorToken.provider == PROVIDER))
    if not row:
        row = ConnectorToken(provider=PROVIDER)
        db.add(row)
    row.access_token = pat
    row.expires_at = datetime.now(timezone.utc) + timedelta(days=36500)
    row.account_name = "PAT"
    db.commit()
    _cache["specs"] = None


def clear(db: Session) -> None:
    row = db.scalar(select(ConnectorToken).where(ConnectorToken.provider == PROVIDER))
    if row:
        db.delete(row); db.commit()
    _cache["specs"] = None


def is_configured() -> bool:
    return bool(resolve_pat())


@asynccontextmanager
async def _session():
    headers = {"Authorization": f"Bearer {resolve_pat()}"}
    async with streamablehttp_client(settings.github_mcp_url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def _fetch_specs() -> tuple[list[dict], set[str]]:
    async with _session() as session:
        resp = await session.list_tools()
    specs, writes = [], set()
    for t in resp.tools:
        specs.append({"type": "function", "function": {
            "name": t.name,
            "description": (t.description or "")[:1024],
            "parameters": t.inputSchema or {"type": "object", "properties": {}},
        }})
        ann = getattr(t, "annotations", None)
        if not (ann and getattr(ann, "readOnlyHint", False)):
            writes.add(t.name)
    return specs, writes


async def tool_specs(refresh: bool = False) -> tuple[list[dict], set[str]]:
    if refresh or _cache["specs"] is None:
        _cache["specs"], _cache["writes"] = await _fetch_specs()
    return _cache["specs"], _cache["writes"]


async def call_tool(name: str, arguments: str) -> str:
    args = json.loads(arguments) if arguments else {}
    async with _session() as session:
        result = await session.call_tool(name, args)
    parts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
    text = "\n".join(parts).strip() or "GitHub completed the action without returning text."
    return text[:4000]


async def get_status() -> dict:
    status = {
        "provider": PROVIDER,
        "name": "GitHub",
        "description": "Search repositories, read files, and manage issues and pull requests through GitHub's remote MCP server.",
        "configured": is_configured(),
        "connected": False,
        "account_name": "",
        "actions": [],
    }
    if not is_configured():
        return status
    try:
        specs, _ = await tool_specs()
        status["connected"] = True
        status["account_name"] = f"PAT configured · {len(specs)} tools"
        status["actions"] = [s["function"]["name"] for s in specs[:6]]
    except Exception as exc:
        status["account_name"] = f"Connection failed: {exc}"[:80]
    return status
