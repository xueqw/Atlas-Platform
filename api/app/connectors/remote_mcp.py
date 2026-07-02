"""Generic MCP connector registry — streamable HTTP, SSE, and stdio."""

import json
import os
import re
import shlex
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.stdio import stdio_client, StdioServerParameters, get_default_environment
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal
from ..models import ConnectorToken


REGISTRY: dict[str, dict] = {
    "baidu_maps": {
        "name": "Baidu Maps",
        "description": (
            "Official Baidu Maps MCP: geocoding, POI search/details, route planning, "
            "weather, IP location, traffic, and text-to-POI extraction."
        ),
        "url": "https://mcp.map.baidu.com/sse?ak={key}",
        "transport": "sse",
        "headers": {},
        "key_label": "Baidu Maps server-side AK (enable MCP/SSE in Baidu Maps Open Platform)",
        "env": "baidu_maps_api_key",
        "requires_key": True,
    },
    "amap": {
        "name": "Amap",
        "description": (
            "Amap MCP: geocoding, reverse geocoding, weather, driving/walking/"
            "cycling/transit routes, distance measurement, POI search, and details."
        ),
        "url": "https://mcp.amap.com/mcp?key={key}",
        "headers": {},
        "key_label": "Amap API Key (apply at lbs.amap.com)",
        "env": "amap_key",
        "requires_key": True,
    },
    "firecrawl": {
        "name": "Firecrawl",
        "description": (
            "Hosted Firecrawl MCP: web search, scraping, extraction, interaction, "
            "crawling, and research tools. Keyless tier supports scrape/search/interact."
        ),
        "url": "https://mcp.firecrawl.dev/v2/mcp",
        "key_url": "https://mcp.firecrawl.dev/{key}/v2/mcp",
        "headers": {},
        "key_label": "Firecrawl API Key (optional for more tools/higher limits)",
        "env": "firecrawl_api_key",
        "requires_key": False,
    },
    "context7": {
        "name": "Context7",
        "description": (
            "Context7 MCP: resolves library IDs and queries up-to-date developer "
            "documentation/code examples. Indexed from MCPWorld/SAI and connected "
            "via the official remote Streamable HTTP endpoint."
        ),
        "url": "https://mcp.context7.com/mcp",
        "headers": {"CONTEXT7_API_KEY": "{key}"},
        "key_label": "Context7 API Key (optional, for higher rate limits)",
        "env": "context7_api_key",
        "requires_key": False,
    },
    "fetcher": {
        "name": "Fetcher",
        "description": (
            "Fetcher MCP: browser-backed web page content fetching with "
            "JavaScript rendering, Markdown/HTML output, and batch URL fetching."
        ),
        "transport": "stdio",
        "command": "node",
        "args_override": [
            str(Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / "fetcher-mcp" / "build" / "index.js")
        ],
        "env_vars": {},
        "key_label": "No key required; connects via stdio to local fetcher-mcp",
        "requires_key": False,
        "config_kind": "local_mcp",
        "setup_hint": "Installed: npm i -g fetcher-mcp. Platform spawns it on demand.",
    },
    "xiaohongshu": {
        "name": "Xiaohongshu",
        "description": (
            "Xiaohongshu MCP: search/read RedNote content via browser automation. "
            "Headless Chromium launches automatically on first use."
        ),
        "transport": "stdio",
        "command": "xiaohongshu-mcp --headless",
        "env_vars": {},
        "key_label": "No key required; connects via stdio to local xiaohongshu-mcp",
        "requires_key": False,
        "config_kind": "local_mcp",
        "setup_hint": "Installed: npm i -g xiaohongshu-mcp. Platform spawns it on demand.",
    },
    "excel": {
        "name": "Excel",
        "description": (
            "Excel MCP: create, read, and modify Excel workbooks via stdio."
        ),
        "transport": "stdio",
        "command": "uvx",
        "args_override": ["excel-mcp-server", "stdio"],
        "key_label": "No key required; connects via stdio to local excel-mcp-server",
        "requires_key": False,
        "config_kind": "local_mcp",
        "setup_hint": "Uses uvx excel-mcp-server. Platform spawns it on demand.",
    },
    "arxiv": {
        "name": "arXiv",
        "description": (
            "arXiv MCP: search and retrieve academic papers from arXiv. "
            "Spawned on demand via stdio."
        ),
        "transport": "stdio",
        "command": "arxiv",
        "env_vars": {},
        "key_label": "No key required; connects via stdio to local arxiv CLI",
        "requires_key": False,
        "config_kind": "local_mcp",
        "setup_hint": "Installed: npm i -g @fre4x/arxiv. Platform spawns it on demand.",
    },
}

_cache: dict[str, dict] = {}

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_EXCEL_OUTPUT_DIR = _PROJECT_ROOT / "output" / "excel"
_READ_ONLY_PROVIDERS = {
    "baidu_maps",
    "amap",
    "arxiv",
    "context7",
    "firecrawl",
    "fetcher",
    "xiaohongshu",
}


def providers() -> list[str]:
    return list(REGISTRY)


def is_known(provider: str) -> bool:
    return provider in REGISTRY


def resolve_key(provider: str) -> str:
    with SessionLocal() as db:
        row = db.scalar(select(ConnectorToken).where(ConnectorToken.provider == provider))
        if row and row.access_token:
            return row.access_token
    env = REGISTRY[provider].get("env")
    return getattr(settings, env, "") if env else ""


def is_configured(provider: str) -> bool:
    cfg = REGISTRY[provider]
    if cfg.get("transport") == "stdio":
        return True
    return not cfg.get("requires_key", True) or bool(resolve_key(provider))


def save_key(db: Session, provider: str, key: str) -> None:
    row = db.scalar(select(ConnectorToken).where(ConnectorToken.provider == provider))
    if not row:
        row = ConnectorToken(provider=provider)
        db.add(row)
    row.access_token = key
    row.expires_at = datetime.now(timezone.utc) + timedelta(days=36500)
    row.account_name = "KEY"
    db.commit()
    _cache.pop(provider, None)


def clear(db: Session, provider: str) -> None:
    row = db.scalar(select(ConnectorToken).where(ConnectorToken.provider == provider))
    if row:
        db.delete(row)
        db.commit()
    _cache.pop(provider, None)


@asynccontextmanager
async def _session(provider: str):
    cfg = REGISTRY[provider]
    key = resolve_key(provider)
    transport = cfg.get("transport", "streamable_http")

    if transport == "stdio":
        cmd = shlex.split(cfg["command"])
        # Allow overriding args for piped tools (e.g. uvx)
        if cfg.get("args_override"):
            cmd = cmd + cfg["args_override"]
        env = get_default_environment()
        env.update(cfg.get("env_vars", {}))
        sp = StdioServerParameters(command=cmd[0], args=cmd[1:], env=env)
        async with stdio_client(sp) as streams:
            read, write = streams
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
        return

    url_template = cfg.get("key_url") if key and cfg.get("key_url") else cfg["url"]
    url = url_template.format(key=key)
    headers = {
        name: value.format(key=key)
        for name, value in cfg.get("headers", {}).items()
        if key or "{key}" not in value
    }
    if transport == "sse":
        client = sse_client(url, headers=headers, timeout=20, sse_read_timeout=60)
    else:
        client = streamablehttp_client(url, headers=headers, timeout=timedelta(seconds=20))
    async with client as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


# Schema overrides for tools with broken/empty inputSchema (e.g. xiaohongshu-mcp zod-to-json-schema bug).
_SCHEMA_OVERRIDES: dict[str, dict] = {
    "browser_navigate": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to navigate to"},
        },
        "required": ["url"],
    },
    "browser_click": {
        "type": "object",
        "properties": {
            "element": {"type": "string", "description": "Human-readable element description"},
            "ref": {"type": "string", "description": "Exact target element reference from the page snapshot"},
        },
        "required": ["element", "ref"],
    },
    "browser_hover": {
        "type": "object",
        "properties": {
            "element": {"type": "string", "description": "Human-readable element description"},
            "ref": {"type": "string", "description": "Exact target element reference from the page snapshot"},
        },
        "required": ["element", "ref"],
    },
    "browser_type": {
        "type": "object",
        "properties": {
            "element": {"type": "string", "description": "Human-readable element description"},
            "ref": {"type": "string", "description": "Exact target element reference from the page snapshot"},
            "text": {"type": "string", "description": "Text to type into the element"},
            "submit": {"type": "boolean", "description": "Whether to submit entered text (press Enter after)"},
        },
        "required": ["element", "ref", "text"],
    },
    "browser_wait": {
        "type": "object",
        "properties": {
            "time": {"type": "number", "description": "The time to wait in seconds"},
        },
        "required": ["time"],
    },
    "browser_press_key": {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Name of the key to press, such as ArrowLeft or a"},
        },
        "required": ["key"],
    },
    "browser_drag": {
        "type": "object",
        "properties": {
            "startElement": {"type": "string", "description": "Human-readable source element description"},
            "startRef": {"type": "string", "description": "Exact source element reference from the page snapshot"},
            "endElement": {"type": "string", "description": "Human-readable target element description"},
            "endRef": {"type": "string", "description": "Exact target element reference from the page snapshot"},
        },
        "required": ["startElement", "startRef", "endElement", "endRef"],
    },
    "browser_move_mouse": {
        "type": "object",
        "properties": {
            "element": {"type": "string", "description": "Human-readable element description"},
            "x": {"type": "number", "description": "X coordinate"},
            "y": {"type": "number", "description": "Y coordinate"},
        },
        "required": ["element", "x", "y"],
    },
}


def _fix_schema(name: str, raw: dict) -> dict:
    """If the tool's schema is broken (empty properties despite having params), apply override."""
    if name in _SCHEMA_OVERRIDES:
        props = raw.get("properties")
        if not props or (isinstance(props, dict) and len(props) == 0):
            return _SCHEMA_OVERRIDES[name]
    return raw


async def _fetch_specs(provider: str) -> tuple[list[dict], set[str]]:
    async with _session(provider) as session:
        resp = await session.list_tools()
    specs, writes = [], set()
    for tool in resp.tools:
        raw_schema = tool.inputSchema or {"type": "object", "properties": {}}
        fixed_schema = _fix_schema(tool.name, raw_schema)
        specs.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": (tool.description or "")[:1024],
                    "parameters": fixed_schema,
                },
            }
        )
        annotations = getattr(tool, "annotations", None)
        is_read_only = (
            provider in _READ_ONLY_PROVIDERS
            or bool(annotations and getattr(annotations, "readOnlyHint", False))
        )
        if not is_read_only:
            writes.add(tool.name)
    return specs, writes


async def tool_specs(provider: str, refresh: bool = False) -> tuple[list[dict], set[str]]:
    if refresh or provider not in _cache:
        specs, writes = await _fetch_specs(provider)
        _cache[provider] = {"specs": specs, "writes": writes}
    cached = _cache[provider]
    return cached["specs"], cached["writes"]


async def call_tool(provider: str, name: str, arguments: str) -> str:
    args = json.loads(arguments) if arguments else {}
    args = _normalize_tool_args(provider, args)
    if provider == "fetcher" and name == "fetch_url":
        return await _fetch_url_fallback(args)
    if provider == "fetcher" and name == "fetch_urls":
        urls = args.get("urls") if isinstance(args, dict) else []
        if isinstance(urls, list):
            results = [await _fetch_url_fallback({"url": url, "maxLength": args.get("maxLength", 4000)}) for url in urls[:5]]
            return "\n\n---\n\n".join(results)[:4000]
    async with _session(provider) as session:
        result = await session.call_tool(name, args)
    parts = [content.text for content in result.content if getattr(content, "type", None) == "text"]
    return ("\n".join(parts).strip() or "(tool executed; no text returned)")[:4000]


class _PageParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title: list[str] = []
        self.text: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._tag_stack: list[str] = []
        self._current_href = ""
        self._current_link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        self._tag_stack.append(tag)
        if tag == "a":
            href = next((value for name, value in attrs if name.lower() == "href" and value), "")
            self._current_href = urljoin(self.base_url, href) if href else ""
            self._current_link_text = []

    def handle_endtag(self, tag: str):
        if tag == "a" and self._current_href:
            label = _clean_text(" ".join(self._current_link_text)) or self._current_href
            self.links.append((label[:120], self._current_href))
            self._current_href = ""
            self._current_link_text = []
        if self._tag_stack:
            self._tag_stack.pop()

    def handle_data(self, data: str):
        if not data.strip() or any(tag in self._tag_stack for tag in ("script", "style", "noscript")):
            return
        if "title" in self._tag_stack:
            self.title.append(data.strip())
        elif self._current_href:
            self._current_link_text.append(data.strip())
        else:
            self.text.append(data.strip())


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _short_url(url: str, max_len: int = 96) -> str:
    parsed = urlparse(url)
    if not parsed.netloc:
        return url[:max_len]
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    compact = f"{parsed.scheme}://{parsed.netloc}{path}{query}"
    if len(compact) <= max_len:
        return compact
    if parsed.query:
        compact = f"{parsed.scheme}://{parsed.netloc}{path}?... "
    return compact[:max_len].rstrip()


def _is_noisy_url(url: str) -> bool:
    parsed = urlparse(url)
    query = parsed.query.lower()
    return len(url) > 160 or "wd=" in query or "rsv_" in query or "tn=" in query


async def _fetch_url_fallback(args: dict) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return "Fetcher fallback error: missing url"
    max_length = int(args.get("maxLength") or 4000)
    headers = {"User-Agent": "Mozilla/5.0 AtlasFetcher/1.0"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers, trust_env=False) as client:
        response = await client.get(url)
        if 'location.replace(location.href.replace("https://","http://"))' in response.text and str(response.url).startswith("https://"):
            response = await client.get(str(response.url).replace("https://", "http://", 1))
    content_type = response.headers.get("content-type", "")
    body = response.text
    if "html" not in content_type.lower():
        return f"URL: {str(response.url)}\nStatus: {response.status_code}\nContent-Type: {content_type}\n\n{body[:max_length]}"
    body = re.sub(r"<(script|style|noscript)\b[^>]*>.*?</\1>", " ", body, flags=re.I | re.S)
    parser = _PageParser(str(response.url))
    parser.feed(body)
    title = _clean_text(" ".join(parser.title)) or "(no title)"
    text_items = []
    seen_text = set()
    for item in parser.text:
        cleaned = _clean_text(item)
        if len(cleaned) < 2 or cleaned in seen_text:
            continue
        if cleaned.lower().startswith("<style") or cleaned.count("{") + cleaned.count("}") > 6:
            continue
        seen_text.add(cleaned)
        text_items.append(cleaned)
        if len(" ".join(text_items)) > max_length:
            break
    link_lines = []
    seen_links = set()
    for label, href in parser.links:
        if href in seen_links or href.lower().startswith(("javascript:", "mailto:")):
            continue
        if _is_noisy_url(href) and len(link_lines) >= 6:
            continue
        seen_links.add(href)
        clean_label = _clean_text(label).strip(" -:")[:60] or "(untitled)"
        link_lines.append(f"- {clean_label}: {_short_url(href)}")
        if len(link_lines) >= 12:
            break
    return (
        f"URL: {str(response.url)}\n"
        f"Status: {response.status_code}\n"
        f"Title: {title}\n\n"
        f"Text:\n{chr(10).join(text_items)[:max_length]}\n\n"
        f"Links:\n{chr(10).join(link_lines) if link_lines else '(none found)'}"
    )[: max_length + 2000]


def _normalize_tool_args(provider: str, args: dict) -> dict:
    if provider != "excel" or not isinstance(args, dict):
        return args
    normalized = dict(args)
    for key in ("filepath", "file_path", "path", "filename"):
        value = normalized.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = _safe_excel_path(value)
    return normalized


def _safe_excel_path(raw: str) -> str:
    value = raw.strip().strip('"')
    candidate = Path(value)
    bad_user_path = value.lower().startswith(("c:\\users\\admin\\", "c:/users/admin/"))
    if not candidate.is_absolute() or bad_user_path:
        _EXCEL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        name = candidate.name or "example.xlsx"
        if not name.lower().endswith((".xlsx", ".xlsm", ".xls")):
            name += ".xlsx"
        return str(_EXCEL_OUTPUT_DIR / name)
    return value


async def get_status(provider: str) -> dict:
    cfg = REGISTRY[provider]
    kind = "local_mcp" if cfg.get("config_kind") == "local_mcp" or cfg.get("transport") == "stdio" else cfg.get("config_kind", "mcp_key")
    status = {
        "provider": provider,
        "name": cfg["name"],
        "description": cfg["description"],
        "configured": is_configured(provider),
        "connected": False,
        "account_name": "",
        "actions": [],
        "config_kind": kind,
        "key_label": cfg.get("key_label", ""),
    }
    if not is_configured(provider):
        return status
    try:
        specs, _ = await tool_specs(provider)
        status["connected"] = True
        key_status = "Key configured" if resolve_key(provider) else "No key required"
        status["account_name"] = f"{key_status} / {len(specs)} tools"
        status["actions"] = [spec["function"]["name"] for spec in specs[:6]]
    except Exception as exc:
        status["account_name"] = cfg.get("setup_hint", f"Connection failed: {exc}")[:160]
    return status
