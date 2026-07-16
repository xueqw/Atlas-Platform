"""Model capability registry.

Single source of truth for which model ids the planner may feed image
content to. The planner attachment pipeline (see planner_attachments.py and
agentscope_runner.run_conversation) consults ``is_multimodal`` before deciding
whether to inject an OpenAI vision content array or fall back to a text-only
notice. Extend this set as new vision-capable models are onboarded; future
modalities (audio, pdf) should grow their own sets/helpers here rather than
sprinkling model id checks across the codebase.
"""

from __future__ import annotations

import os

# Default runtime model for newly created planner sessions / agent nodes. Keep this
# on a model that is available through the configured GLM gateway and verified for
# planner/function-calling flows.
DEFAULT_CHAT_MODEL_ID = "glm-4-flash"
DEFAULT_CHAT_PROVIDER = "glm"

# Model ids (or id prefixes) that accept image input via the OpenAI-compatible
# vision content array. Matching is prefix-based so versioned variants like
# ``gpt-4o-2024-08-06`` resolve to ``gpt-4o``.
MULTIMODAL_MODELS: set[str] = {
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4-vision-preview",
    "qwen-vl-plus",
    "qwen-vl-max",
    "glm-4v",
    "glm-4v-plus",
}


def is_multimodal(model_id: str) -> bool:
    """Return True when ``model_id`` supports image input.

    Prefix match: any model id that starts with a known multimodal entry counts
    as multimodal, so date-stamped or fine-tuned variants inherit capability.
    """
    if not model_id:
        return False
    mid = model_id.strip().lower()
    return any(mid == m or mid.startswith(m) for m in MULTIMODAL_MODELS)


# Models verified to return STRUCTURED tool_calls through the new-api gateway
# (planner-agentic-react-loop spike, 2026-06). Others — notably qwen-turbo —
# "fake" tool calls as plain text and must NOT run the agentic loop. This is an
# explicit allowlist rather than a live probe: the probe is slow per-turn and the
# gateway's model roster is small and known. Extend after verifying a new model
# actually emits `finish_reason: tool_calls`.
FUNCTION_CALLING_MODELS: frozenset[str] = frozenset({
    "glm-4-flash",
})


def supports_function_calling(model_id: str) -> bool:
    """Whether ``model_id`` returns structured tool_calls (real function-calling)
    through the gateway. Gates the agentic ReAct planner path; unknown/false →
    caller falls back to the single-call path. Exact match (no prefix) — variants
    must be verified individually.

    Testing escape hatch: ``PLANNER_AGENTIC_ALL=on`` forces True for ANY model so
    the agentic loop can be exercised across the whole model roster during manual
    QA. Read per-call (toggle without restart). Leave OFF in production — models
    not on the allowlist may "fake" tool calls as plain text."""
    if (os.environ.get("PLANNER_AGENTIC_ALL") or "off").strip().lower() in {"1", "on", "true", "yes"}:
        return bool(model_id and model_id.strip())
    if not model_id:
        return False
    return model_id.strip().lower() in FUNCTION_CALLING_MODELS


# Prefix → provider fallback, consulted only when a model id is absent from the
# ModelRegistry. Ordered longest-prefix-first at match time. Keep this aligned
# with the providers wired in app.core.config.PROVIDER_CONFIG.
_PROVIDER_PREFIXES: tuple[tuple[str, str], ...] = (
    ("gpt-", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("claude", "anthropic"),
    ("deepseek", "deepseek"),
    ("qwen", "openai"),
    ("glm", "glm"),
    ("doubao", "glm"),
    ("kimi", "glm"),
    ("minimax", "glm"),
    ("step", "glm"),
)


def provider_for_model(model_id: str, session=None, default: str = "glm") -> str:
    """Resolve the authoritative provider for a model id.

    The proposal JSON the planner LLM emits often guesses ``provider`` wrong
    (e.g. tagging an OpenAI model as ``glm``), which routes the call to the
    wrong gateway. We never trust that field: the capability library
    (``capability_items`` type=model) is the single source of truth, then a
    prefix heuristic, then ``default``.

    ``session`` (a SQLModel Session) enables the capability lookup; without one
    only the prefix heuristic runs, so this stays safe to call from pure
    contexts. Matching is case-insensitive and prefix-based so versioned ids
    like ``gpt-4o-2024-08-06`` resolve to ``openai``.
    """
    if not model_id:
        return default
    mid = model_id.strip()
    low = mid.lower()

    if session is not None:
        cfg = _capability_model_config(session, mid)
        if cfg and cfg.get("provider"):
            return str(cfg["provider"])

    for prefix, provider in _PROVIDER_PREFIXES:
        if low.startswith(prefix):
            return provider
    return default


def _capability_model_config(session, model_id: str) -> dict | None:
    """Return the config dict of the capability(type=model) whose
    ``config.model_id`` matches ``model_id``, or None. Single source of truth
    for model metadata after unify-model-source-to-capability."""
    import json
    try:
        from sqlmodel import select
        from app.models.db import CapabilityItem
        for cap in session.exec(select(CapabilityItem).where(CapabilityItem.type == "model")).all():
            try:
                cfg = json.loads(cap.config) if isinstance(cap.config, str) else (cap.config or {})
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(cfg, dict) and cfg.get("model_id") == model_id:
                return cfg
    except Exception:
        # DB unavailable / shape mismatch — caller falls back to heuristic.
        pass
    return None


def _capability_endpoint(cfg: dict) -> dict:
    """Extract a capability-library model's custom endpoint from its config.

    Returns a dict with ``base_url``/``api_key`` keys present only when the
    config actually carries *valid* (non-placeholder) values. Placeholder
    values are treated as absent so that the provider's env credentials are
    used as fallback. Built-in registry models declare neither and route via
    the provider's env credentials. Credentials stay backend-only and MUST NOT
    be forwarded to the frontend.
    """
    from app.core.config import looks_like_placeholder

    out: dict = {}
    if not isinstance(cfg, dict):
        return out
    base_url = cfg.get("base_url")
    api_key = cfg.get("api_key")
    if base_url and not looks_like_placeholder(str(base_url)):
        out["base_url"] = base_url
    if api_key and not looks_like_placeholder(str(api_key)):
        out["api_key"] = api_key
    return out


def resolve_model_endpoint(model_name: str, session=None, default: str = "glm") -> dict:
    """Resolve a ``model_name`` to its authoritative routing.

    Returns ``{"provider": ..., "base_url"?: ..., "api_key"?: ...}``:

    - ``provider``: the capability-library model's declared ``config.provider``
      (single source of truth after unify-model-source-to-capability); otherwise
      the prefix heuristic / default.
    - ``base_url``/``api_key`` are populated ONLY when the capability model
      (matched by ``config.model_id``) carries a custom endpoint. Built-in models
      carry neither and route via the provider's env credentials.

    ``session`` enables the capability lookup; without one only the prefix
    heuristic runs (provider-only), keeping this safe to call from pure contexts.
    Credentials stay backend-only and MUST NOT reach the frontend.
    """
    if not model_name or session is None:
        return {"provider": provider_for_model(model_name, session=session, default=default)}

    cfg = _capability_model_config(session, model_name)
    if cfg:
        result = {"provider": cfg.get("provider") or provider_for_model(model_name, default=default)}
        result.update(_capability_endpoint(cfg))
        return result
    return {"provider": provider_for_model(model_name, session=session, default=default)}
