import asyncio
import json
import time
import httpx
from .config import settings

SYSTEM_PROMPT = "You are a careful enterprise AI assistant. Ground answers in the connected knowledge base, cite relevant sources, and clearly state when the available information is insufficient."

DEFAULT_MODEL = settings.openai_model

# Single source of truth for model routing and the provider settings screen.
PROVIDERS = [
    {
        "id": "openai",
        "name": "OpenAI",
        "base_url": settings.openai_base_url,
        "api_key": settings.openai_api_key,
        "prefix": "gpt",
        "models": [settings.openai_model],
        "note": "Primary",
    },
]


def resolve_provider(model: str | None) -> tuple[str, str, str]:
    """Route a model name to its provider and return (base_url, api_key, model)."""
    for p in PROVIDERS:
        if model and (model in p["models"] or (p["prefix"] and model.lower().startswith(p["prefix"]))):
            return p["base_url"], p["api_key"], model
    return settings.openai_base_url, settings.openai_api_key, (model or settings.openai_model)


def list_providers() -> dict:
    """List providers and configuration status for the model settings page."""
    return {
        "default": DEFAULT_MODEL,
        "providers": [
            {
                "id": p["id"],
                "name": p["name"],
                "base_url": p["base_url"],
                "configured": bool(p["api_key"]),
                "models": p["models"],
                "note": p["note"],
            }
            for p in PROVIDERS
        ],
    }


async def test_model(model: str) -> dict:
    """Send a minimal request to test provider connectivity and latency."""
    base_url, api_key, real_model = resolve_provider(model)
    if not api_key:
        return {"ok": False, "message": "This provider does not have an API key configured."}
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": real_model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            r = await client.post(url, headers=headers, json=payload)
        latency = int((time.perf_counter() - started) * 1000)
        if r.status_code == 200:
            return {"ok": True, "latency_ms": latency}
        return {"ok": False, "latency_ms": latency, "message": f"HTTP {r.status_code}: {r.text[:160]}"}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


async def stream_model(messages: list[dict], model: str | None = None):
    base_url, api_key, real_model = resolve_provider(model)
    if not api_key:
        text = demo_answer(messages)
        for char in text:
            yield char
            await asyncio.sleep(0.008)
        return
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": real_model, "stream": True, "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages[-14:]]}
    async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                data = json.loads(line[6:])
                token = data.get("choices", [{}])[0].get("delta", {}).get("content")
                if token:
                    yield token


async def embed_texts(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """Embed text with an OpenAI-compatible endpoint; return [] for BM25 fallback."""
    if not texts:
        return []
    if settings.embedding_api_key or settings.embedding_base_url:
        api_key = settings.embedding_api_key or settings.openai_api_key
        base_url = settings.embedding_base_url or settings.openai_base_url
        model = settings.embedding_model or "text-embedding-3-small"
    elif settings.openai_api_key:
        api_key = settings.openai_api_key
        base_url = settings.openai_base_url
        model = settings.embedding_model or "text-embedding-3-small"
    else:
        return []
    url = base_url.rstrip("/") + "/embeddings"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        for start in range(0, len(texts), batch_size):
            batch = texts[start:start + batch_size]
            payload = {"model": model, "input": batch}
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = sorted(response.json()["data"], key=lambda item: item["index"])
            vectors.extend(item["embedding"] for item in data)
    return vectors


async def embed_query(text: str) -> list[float] | None:
    """Embed one query, returning None to trigger BM25 when unavailable."""
    try:
        vectors = await embed_texts([text])
    except Exception:
        return None
    return vectors[0] if vectors else None


async def stream_agent(messages: list[dict], model: str | None, tool_specs: list[dict], execute_tool, needs_confirm=None, max_rounds: int = 4):
    """Stream a model response and execute approved tool calls."""
    needs_confirm = needs_confirm or (lambda _name: False)
    base_url, api_key, real_model = resolve_provider(model)
    if not api_key:
        for ch in demo_answer(messages):
            yield {"type": "token", "content": ch}
        return
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    convo = [{"role": "system", "content": SYSTEM_PROMPT}, *messages]

    for _ in range(max_rounds):
        payload = {"model": real_model, "stream": True, "messages": convo}
        if tool_specs:
            payload["tools"] = tool_specs
        tool_calls: dict[int, dict] = {}
        finish = None
        async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    choice = json.loads(line[6:]).get("choices", [{}])[0]
                    delta = choice.get("delta", {})
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
                    token = delta.get("content")
                    if token:
                        yield {"type": "token", "content": token}
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = tool_calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]

        if finish != "tool_calls" or not tool_calls:
            return

        calls = [tool_calls[i] for i in sorted(tool_calls)]
        # Write operations pause for explicit user confirmation.
        for c in calls:
            if needs_confirm(c["name"]):
                yield {"type": "confirm_required", "name": c["name"], "args": c["arguments"]}
                return
        # Read-only tools execute immediately and feed results back to the model.
        convo.append({"role": "assistant", "content": None,
                      "tool_calls": [{"id": c["id"], "type": "function",
                                      "function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]})
        for c in calls:
            yield {"type": "tool_call", "name": c["name"], "args": c["arguments"]}
            result = await execute_tool(c["name"], c["arguments"])
            yield {"type": "tool_result", "name": c["name"], "content": result}
            convo.append({"role": "tool", "tool_call_id": c["id"], "content": result})
    # Stop if the model reaches the tool-call round limit.


def demo_answer(messages: list[dict]) -> str:
    question = messages[-1]["content"]
    context = next((item["content"] for item in reversed(messages) if item["role"] == "system" and item["content"].startswith("Knowledge base sources")), "")
    if context:
        excerpt = context.split("\n", 2)[-1][:420]
        return f"Based on the connected knowledge base: {excerpt}\n\nThis excerpt comes from local retrieval. Configure a model API key for a complete synthesized answer."
    if any(word in question.lower() for word in ("enterprise", "features", "permissions")):
        return "The enterprise workspace supports private knowledge bases, centralized model access, tool calls, access controls, and audit-ready workflows."
    return "The FastAPI service received and saved this task. Connect a knowledge base to ground future answers in your own source material."
