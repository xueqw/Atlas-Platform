import asyncio
import json
import httpx
from .config import settings

SYSTEM_PROMPT = "你是一名严谨、清晰的企业智能助手。简洁回答用户问题；资料不足时明确说明。"


async def stream_model(messages: list[dict]):
    if not settings.openai_api_key:
        text = demo_answer(messages[-1]["content"])
        for char in text:
            yield char
            await asyncio.sleep(0.012)
        return

    url = settings.openai_base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"}
    payload = {"model": settings.openai_model, "stream": True, "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages[-12:]]}
    async with httpx.AsyncClient(timeout=90) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                data = json.loads(line[6:])
                token = data.get("choices", [{}])[0].get("delta", {}).get("content")
                if token:
                    yield token


def demo_answer(question: str) -> str:
    if any(word in question for word in ("企业版", "功能", "权限")):
        return "企业版支持私有知识库、模型统一接入、工具调用、权限管理与运行审计。当前回答来自迁移后的 FastAPI 本地演示模型。"
    if any(word in question for word in ("时间", "客服", "服务")):
        return "标准服务时间为工作日 9:00–18:00。接入知识库后，这里会同时返回文件与段落引用。"
    return "任务已经由新的 FastAPI 会话服务接收并持久化。配置 OPENAI_API_KEY 后，将自动切换到真实模型的流式回答。"
