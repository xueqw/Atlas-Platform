"""GitHub 连接器：通过官方远程 MCP Server 动态接入 GitHub 工具。

平台在这里扮演 MCP 客户端：连上 https://api.githubcopilot.com/mcp/（PAT 认证），
tools/list 拿到 GitHub 的全部工具，转成 OpenAI 兼容声明给模型；模型调用时 tools/call 转发。
不需要为每个 GitHub 操作手写代码——这就是"路线 B"（MCP 当统一协议）。
"""
import json
from contextlib import asynccontextmanager

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from ..config import settings

PROVIDER = "github"
_cache: dict = {"specs": None, "writes": None}  # 工具清单缓存，避免每条消息都重新拉


def is_configured() -> bool:
    return bool(settings.github_pat)


@asynccontextmanager
async def _session():
    headers = {"Authorization": f"Bearer {settings.github_pat}"}
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
        if not (ann and getattr(ann, "readOnlyHint", False)):  # 非只读 = 写操作，需确认
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
    text = "\n".join(parts).strip() or "（GitHub 工具已执行，无文本返回）"
    return text[:4000]


async def get_status() -> dict:
    status = {
        "provider": PROVIDER,
        "name": "GitHub",
        "description": "通过官方 MCP Server 接入：搜索仓库、读文件、管理 issue / PR 等，工具由 MCP 动态提供。",
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
        status["account_name"] = f"PAT 已配置 · {len(specs)} 个工具"
        status["actions"] = [s["function"]["name"] for s in specs[:6]]
    except Exception as exc:
        status["account_name"] = f"连接失败：{exc}"[:80]
    return status
