import asyncio
import json
import time
from pathlib import Path
import httpx
from .config import settings

SYSTEM_PROMPT = (
    "你是一名严谨、清晰的企业智能助手，通过调用工具获取实时数据来完成用户请求。\n\n"
    "## 工具调用铁律（最高优先级——违反则回答无效）\n"
    "1. 当系统提供了工具（函数调用），涉及搜索、查询、获取网页/地图/论文/小红书/外部数据时，"
    "必须调用对应工具获取真实结果。严禁使用训练数据编造回答。\n"
    "2. 工具返回的内容是唯一可信来源。如实转述结果；结果为空或出错时如实告知，不自编内容填补。\n"
    "3. 即使用户问题很简单，只要有匹配的工具可用就必须先调用——不得跳过工具直接生成答案。\n"
    "4. 调用工具时使用中文关键词/参数，确保搜索结果准确。\n\n"
    "## 知识库规则\n"
    "仅在无对应工具可用时，才退而参考知识库资料。资料不足时明确指出。"
)

DEFAULT_MODEL = "glm-4-flash"

# 供应商注册表：模型路由、模型管理页、连接测试的唯一数据源。
PROVIDERS = [
    {
        "id": "zhipu",
        "name": "智谱 AI",
        "base_url": settings.zhipu_base_url,
        "api_key": settings.zhipu_api_key,
        "prefix": "glm",
        "models": ["glm-4-flash", "glm-4.5-flash"],
        "note": "免费",
    },
    {
        "id": "aliyun",
        "name": "阿里云百炼",
        "base_url": settings.openai_base_url,
        "api_key": settings.openai_api_key,
        "prefix": "qwen",
        "models": ["qwen-turbo", "qwen-plus", "qwen-max"],
        "note": "通义千问",
    },
]

PROVIDER_ENV = {
    "zhipu": {"api_key": "ZHIPU_API_KEY", "base_url": "ZHIPU_BASE_URL", "settings_key": "zhipu_api_key", "settings_base_url": "zhipu_base_url"},
    "aliyun": {"api_key": "OPENAI_API_KEY", "base_url": "OPENAI_BASE_URL", "settings_key": "openai_api_key", "settings_base_url": "openai_base_url"},
}

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def resolve_provider(model: str | None) -> tuple[str, str, str]:
    """根据模型名路由到对应供应商，返回 (base_url, api_key, model)。"""
    for p in PROVIDERS:
        if model and (model in p["models"] or (p["prefix"] and model.lower().startswith(p["prefix"]))):
            return p["base_url"], p["api_key"], model
    return settings.openai_base_url, settings.openai_api_key, (model or settings.openai_model)


def list_providers() -> dict:
    """模型管理页用：列出所有供应商、各自模型、是否已配置 key。"""
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


def _write_env_value(path: Path, key: str, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    prefix = f"{key}="
    rendered = f'{key}="{value.replace(chr(34), chr(92) + chr(34))}"'
    updated = False
    next_lines = []
    for line in lines:
        if line.startswith(prefix):
            next_lines.append(rendered)
            updated = True
        else:
            next_lines.append(line)
    if not updated:
        next_lines.append(rendered)
    path.write_text("\n".join(next_lines) + "\n", encoding="utf-8")


def configure_provider(provider_id: str, api_key: str, base_url: str | None = None) -> dict:
    meta = PROVIDER_ENV.get(provider_id)
    provider = next((item for item in PROVIDERS if item["id"] == provider_id), None)
    if not meta or not provider:
        raise ValueError("未知模型供应商")
    cleaned_key = api_key.strip()
    if not cleaned_key:
        raise ValueError("API Key 不能为空")
    _write_env_value(ENV_FILE, meta["api_key"], cleaned_key)
    setattr(settings, meta["settings_key"], cleaned_key)
    provider["api_key"] = cleaned_key
    if base_url and base_url.strip():
        cleaned_url = base_url.strip().rstrip("/")
        _write_env_value(ENV_FILE, meta["base_url"], cleaned_url)
        setattr(settings, meta["settings_base_url"], cleaned_url)
        provider["base_url"] = cleaned_url
    return list_providers()


async def test_model(model: str) -> dict:
    """对指定模型发一次最小请求，测连通性与延迟。"""
    base_url, api_key, real_model = resolve_provider(model)
    if not api_key:
        return {"ok": False, "message": "该供应商未配置 API Key（当前为演示模式）"}
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


async def complete(messages: list[dict], model: str | None = None, *, temperature: float = 0.0,
                   max_tokens: int = 600) -> str:
    """非流式补全，返回完整文本。供 Strategy Agent 等内部规划用。
    未配置 key（演示模式）返回空串，让调用方走确定性回退；不注入对话用 SYSTEM_PROMPT。"""
    base_url, api_key, real_model = resolve_provider(model)
    if not api_key:
        return ""
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": real_model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    async with httpx.AsyncClient(timeout=40, trust_env=False) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return r.json().get("choices", [{}])[0].get("message", {}).get("content") or ""


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
    # trust_env=False：国内 API 直连，不走系统代理（如 Clash），避免被代理拦截
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
    """把多段文本转成向量（硅基流动 bge-m3）。未配置 key 时返回空列表，让调用方回退关键词检索。"""
    if not settings.siliconflow_api_key or not texts:
        return []
    url = settings.siliconflow_base_url.rstrip("/") + "/embeddings"
    headers = {"Authorization": f"Bearer {settings.siliconflow_api_key}", "Content-Type": "application/json"}
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        for start in range(0, len(texts), batch_size):
            batch = texts[start:start + batch_size]
            payload = {"model": settings.embedding_model, "input": batch}
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = sorted(response.json()["data"], key=lambda item: item["index"])
            vectors.extend(item["embedding"] for item in data)
    return vectors


async def embed_query(text: str) -> list[float] | None:
    """单条文本转向量；失败或未配置时返回 None，触发关键词回退。"""
    try:
        vectors = await embed_texts([text])
    except Exception:
        return None
    return vectors[0] if vectors else None


async def stream_agent(messages: list[dict], model: str | None, tool_specs: list[dict], execute_tool, needs_confirm=None, max_rounds: int = 4, force_tool_name: str | None = None):
    """带工具调用的对话循环。yield 事件 dict：
    {type:token, content} / {type:tool_call, name, args} / {type:tool_result, name, content}
    / {type:confirm_required, name, args}（写操作，停下等用户确认）。
    模型决定调工具→（写操作先确认）→执行→把结果塞回→再问，直到给出文字回答或达到轮数上限。
    """
    needs_confirm = needs_confirm or (lambda _name: False)
    base_url, api_key, real_model = resolve_provider(model)
    if not api_key:  # 演示模式：无 key，退化成纯文本
        for ch in demo_answer(messages):
            yield {"type": "token", "content": ch}
        return
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    convo = [{"role": "system", "content": SYSTEM_PROMPT}, *messages]

    for _ in range(max_rounds):
        payload = {"model": real_model, "stream": True, "messages": convo}
        if tool_specs:  # 没有启用任何连接器时就当普通对话，不带 tools 字段
            payload["tools"] = tool_specs
            if force_tool_name and _ == 0:
                payload["tool_choice"] = {"type": "function", "function": {"name": force_tool_name}}
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
            return  # 模型给出最终回答，结束

        calls = [tool_calls[i] for i in sorted(tool_calls)]
        # 写操作：停下，发确认请求给上层，本轮不执行
        for c in calls:
            if needs_confirm(c["name"]):
                yield {"type": "confirm_required", "name": c["name"], "args": c["arguments"]}
                return
        # 只读工具：直接执行并把结果回填，继续循环
        convo.append({"role": "assistant", "content": None,
                      "tool_calls": [{"id": c["id"], "type": "function",
                                      "function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]})
        for c in calls:
            yield {"type": "tool_call", "name": c["name"], "args": c["arguments"]}
            result = await execute_tool(c["name"], c["arguments"])
            yield {"type": "tool_result", "name": c["name"], "content": result}
            convo.append({"role": "tool", "tool_call_id": c["id"], "content": result})
    # 达到轮数上限仍在调工具，兜底结束


def demo_answer(messages: list[dict]) -> str:
    question = messages[-1]["content"]
    context = next((item["content"] for item in reversed(messages) if item["role"] == "system" and item["content"].startswith("知识库资料")), "")
    if context:
        excerpt = context.split("\n", 2)[-1][:420]
        return f"根据已连接的知识库资料：{excerpt}\n\n以上内容来自本地检索结果。配置模型密钥后会生成更完整的归纳回答。"
    if any(word in question for word in ("企业版", "功能", "权限")):
        return "企业版支持私有知识库、模型统一接入、工具调用、权限管理与运行审计。"
    return "任务已由 FastAPI 会话服务接收并持久化。你可以连接知识库，让回答基于自己的资料。"
