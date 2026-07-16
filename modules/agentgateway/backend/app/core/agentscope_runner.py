from typing import AsyncIterator, Tuple, Optional, List


async def _run_openai_compatible_stream(
    *,
    base_url: str,
    api_key: str,
    model_name: str,
    system_prompt: str,
    user_content,
    temperature: float,
    max_tokens: int,
) -> AsyncIterator[Tuple[str, str]]:
    """Stream from OpenAI-compatible providers without AgentScope mediation.

    GLM and DashScope both expose this protocol. Keeping this small adapter in
    the runtime prevents a provider-specific AgentScope stream from leaving a
    planner WebSocket open forever when it fails to finish an SSE response.
    """
    import json
    import httpx

    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    timeout = httpx.Timeout(connect=15.0, read=75.0, write=30.0, pool=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                thinking = delta.get("reasoning_content") or delta.get("thinking") or ""
                if thinking:
                    yield ("thinking_content", str(thinking))
                content = delta.get("content") or ""
                if isinstance(content, list):
                    content = "".join(
                        part.get("text", "") if isinstance(part, dict) else str(part)
                        for part in content
                    )
                if content:
                    yield ("token", str(content))
    yield ("done", "")


def _split_attachment_path(path: str) -> Optional[Tuple[str, str]]:
    """Parse "{cid}/attachments/{stored}" → (conversation_id, stored_name)."""
    parts = (path or "").split("/")
    if len(parts) == 3 and parts[1] == "attachments":
        return parts[0], parts[2]
    return None


def _build_user_turn(user_message: str, attachments: Optional[List[dict]], model_name: str):
    """Assemble the user-turn content for the model.

    Returns either a plain string (text-only turn) or a list of content blocks
    (multimodal turn). Text/markdown attachments are always inlined into the
    user text. Image attachments are injected as vision content blocks when the
    model is multimodal; otherwise a degradation notice is appended instead.
    """
    from app.core.model_caps import is_multimodal
    from app.core import planner_attachments as pa
    from agentscope.message import TextBlock, DataBlock, Base64Source

    attachments = attachments or []
    text_parts: List[str] = [user_message] if user_message else []
    image_atts: List[dict] = []

    for att in attachments:
        kind = att.get("kind")
        path = att.get("path") or ""
        resolved = _split_attachment_path(path)
        if kind == "text" and resolved is not None:
            cid, stored = resolved
            body = pa.read_attachment_text(cid, stored)
            if body is not None:
                name = att.get("name") or stored
                text_parts.append(f"\n\n---file: {name}---\n{body}")
        elif kind == "image":
            image_atts.append(att)

    user_text = "".join(text_parts)
    multimodal = is_multimodal(model_name)

    if image_atts and not multimodal:
        user_text = (user_text + f"\n\n（已附图 {len(image_atts)} 张，但当前模型不支持图像，已忽略）").lstrip("\n")

    if image_atts and multimodal:
        blocks: List = [TextBlock(type="text", text=user_text or "")]
        for att in image_atts:
            resolved = _split_attachment_path(att.get("path") or "")
            if resolved is None:
                continue
            cid, stored = resolved
            mime = att.get("mime") or "image/png"
            data_url = pa.attachment_data_url(cid, stored, mime)
            if data_url is None:
                continue
            # data_url = "data:<mime>;base64,<data>" — strip the prefix for Base64Source.
            b64 = data_url.split(",", 1)[1] if "," in data_url else ""
            blocks.append(
                DataBlock(type="data", source=Base64Source(type="base64", media_type=mime, data=b64))
            )
        return blocks

    return user_text


def create_agent(
    system_prompt: str,
    model_name: str,
    provider: str = "openai",
    stream: bool = True,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    tools: Optional[List[dict]] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    toolkit=None,
    react_max_iters: int = 8,
):
    """Create an AgentScope 2.0 Agent from P+M config.

    ``base_url``/``api_key`` override the provider's env-var credentials when
    provided (capability-library custom models carry their own endpoint). When
    omitted, the provider defaults from ``get_provider_credentials`` are used,
    so built-in ModelRegistry models behave exactly as before.

    ``toolkit`` (AgentScope ``Toolkit``) wires the agentic ReAct path: when
    provided, the agent can call the registered tools in a reasoning-acting loop
    (planner-agentic-react-loop). When omitted, behaviour is unchanged — a plain
    single-call agent for ``run_conversation``.
    """
    from agentscope.agent import Agent
    from agentscope.model import OpenAIChatModel
    from agentscope.credential import OpenAICredential

    from app.core.config import get_provider_credentials

    creds = get_provider_credentials(provider)
    resolved_api_key = api_key or creds["api_key"]
    resolved_base_url = base_url or creds.get("base_url") or None
    credential = OpenAICredential(
        api_key=resolved_api_key,
        base_url=resolved_base_url,
    )

    parameters = OpenAIChatModel.Parameters(
        temperature=temperature,
        max_tokens=max_tokens,
        thinking_enable=False,
    )

    model = OpenAIChatModel(
        credential=credential,
        model=model_name,
        parameters=parameters,
        stream=stream,
    )

    agent_kwargs = {
        "name": "factory-agent",
        "system_prompt": system_prompt,
        "model": model,
    }
    if toolkit is not None:
        from agentscope.agent import ReActConfig
        agent_kwargs["toolkit"] = toolkit
        agent_kwargs["react_config"] = ReActConfig(max_iters=react_max_iters)
        # The planner's gathering tools are all read-only; run in BYPASS so the
        # ReAct loop auto-approves tool execution instead of stalling on a
        # human tool-confirmation round-trip (RequireUserConfirmEvent).
        try:
            from agentscope.state import AgentState
            from agentscope.permission import PermissionContext, PermissionMode
            agent_kwargs["state"] = AgentState(
                permission_context=PermissionContext(mode=PermissionMode.BYPASS)
            )
        except Exception:
            pass
    agent = Agent(**agent_kwargs)
    # Stash the provider so run_conversation can attribute generations without
    # re-deriving it from the credential's base_url.
    try:
        agent._factory_provider = provider
        agent._factory_base_url = resolved_base_url
        agent._factory_api_key = resolved_api_key
        agent._factory_temperature = temperature
        agent._factory_max_tokens = max_tokens
    except Exception:
        pass
    return agent


async def run_conversation(
    agent,
    user_message: str,
    attachments: Optional[List[dict]] = None,
) -> AsyncIterator[Tuple[str, str]]:
    """Run one turn of conversation with streaming. Separates think/response.

    When ``attachments`` is provided, text attachments are inlined into the
    user turn and image attachments are injected as vision content blocks for
    multimodal models (non-multimodal models get a text degradation notice).
    """
    import re
    from agentscope.message import UserMsg, SystemMsg
    from agentscope.model import OpenAIChatModel
    from app.core.observability import with_generation, record_generation_usage

    # Access the model directly for streaming
    model = agent.model
    system_prompt = agent._system_prompt
    model_name = getattr(model, "model", "") or ""

    user_content = _build_user_turn(user_message, attachments, model_name)

    # The application-development workbench currently exposes GLM and Qwen,
    # both through OpenAI-compatible endpoints. Use the native SSE adapter so a
    # malformed or unfinished third-party AgentScope stream cannot strand the
    # browser's planner WebSocket in a permanent loading state.
    provider = getattr(agent, "_factory_provider", "") or ""
    base_url = getattr(agent, "_factory_base_url", None)
    api_key = getattr(agent, "_factory_api_key", None)
    if provider in {"glm", "openai"} and base_url and api_key:
        async for event in _run_openai_compatible_stream(
            base_url=base_url,
            api_key=api_key,
            model_name=model_name,
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=getattr(agent, "_factory_temperature", 0.7),
            max_tokens=getattr(agent, "_factory_max_tokens", 4096),
        ):
            yield event
        return

    messages = [
        SystemMsg(name="system", content=system_prompt),
        UserMsg(name="user", content=user_content),
    ]

    # Open a Langfuse generation observation under the active node span (or the
    # current trace, in the planner case). No-op when Langfuse is unconfigured.
    provider = getattr(agent, "_factory_provider", "") or ""
    gen_input = [
        {"role": "system", "content": str(system_prompt)[:4000]},
        {"role": "user", "content": str(user_content)[:4000]},
    ]

    full_text = ""
    usage_in = 0
    usage_out = 0

    with with_generation(name="agent-llm", model=model_name, provider=provider, input=gen_input) as gen:
        stream = await model(messages)

        buffer = ""
        in_thinking = True
        think_sent = False
        last_usage = None

        async for chunk in stream:
            if getattr(chunk, "usage", None) is not None:
                last_usage = chunk.usage
            if chunk.is_last:
                break
            for block in chunk.content:
                if block.type == "text":
                    text = block.text
                    buffer += text

                    if in_thinking:
                        if "</think>" in buffer:
                            parts = buffer.split("</think>", 1)
                            think_content = parts[0]
                            after = parts[1]
                            if not think_sent:
                                yield ("thinking_content", think_content)
                                think_sent = True
                            in_thinking = False
                            buffer = after
                            if buffer.strip():
                                full_text += buffer
                                yield ("token", buffer)
                                buffer = ""
                    else:
                        full_text += text
                        yield ("token", text)
                        buffer = ""
                elif block.type == "thinking":
                    yield ("thinking_content", block.thinking)

        # If no </think> found, the whole buffer is the response
        if in_thinking and buffer:
            cleaned = re.sub(r"^[\s\S]*?</think>\s*", "", buffer)
            emitted = cleaned.strip() if cleaned != buffer else buffer.strip()
            full_text += emitted
            yield ("token", emitted)

        if last_usage is not None:
            usage_in = getattr(last_usage, "input_tokens", 0) or 0
            usage_out = getattr(last_usage, "output_tokens", 0) or 0
        if gen is not None:
            try:
                gen.update(
                    output=full_text,
                    usage_details={"input": usage_in, "output": usage_out},
                )
            except Exception:
                pass
        record_generation_usage(usage_in, usage_out)

    yield ("done", "")
