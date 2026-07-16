"""Load configuration from environment variables."""

import os
from pathlib import Path
from typing import Optional


def _load_dotenv():
    """Load .env file from the backend directory."""
    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key not in os.environ:
                    os.environ[key] = value


_load_dotenv()


PROVIDER_CONFIG = {
    "openai": {
        "api_key_env": "OPENAI_API_KEY",
        "base_url_env": "OPENAI_BASE_URL",
    },
    "glm": {
        "api_key_env": "GLM_API_KEY",
        "base_url_env": "GLM_BASE_URL",
    },
    "anthropic": {
        "api_key_env": "ANTHROPIC_API_KEY",
        "base_url_env": None,
    },
    "deepseek": {
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url_env": "DEEPSEEK_BASE_URL",
    },
    "custom": {
        "api_key_env": "CUSTOM_API_KEY",
        "base_url_env": "CUSTOM_BASE_URL",
    },
}


def get_provider_credentials(provider: str) -> dict:
    """Get API key and base URL for a provider."""
    cfg = PROVIDER_CONFIG.get(provider, {})
    api_key = os.environ.get(cfg.get("api_key_env", ""), "")
    base_url_env = cfg.get("base_url_env")
    base_url = os.environ.get(base_url_env) if base_url_env else None
    return {"api_key": api_key, "base_url": base_url}


# Exact placeholder values shipped in .env.example — any key equal to one of
# these is definitively unconfigured. Kept here (the lowest-level config module)
# so both credential_gap and credential_health share one source of truth.
_EXAMPLE_PLACEHOLDERS = {
    "sk-your-openai-api-key",
    "sk-your-glm-api-key",
    "sk-ant-xxx",
    "sk-xxx",
    "sk-lf-...",
    "pk-lf-...",
    "__set_via_env_only__",
}


def looks_like_placeholder(value: str) -> bool:
    """Heuristic: non-empty but almost certainly not a real key.

    Conservative — only flags values very unlikely to be valid, so a real key is
    never mislabeled: equals a known .env.example sample; an ``sk-``-prefixed key
    of length <= 16 (the observed bad stubs are 13 chars); or an all-``x`` body.
    """
    v = (value or "").strip()
    if not v:
        return False
    if v in _EXAMPLE_PLACEHOLDERS:
        return True
    low = v.lower()
    if low.startswith("sk-") and len(v) <= 16:
        return True
    body = low.replace("sk-", "", 1).replace("pk-", "", 1).replace("-", "").replace("_", "")
    if body and set(body) <= {"x"}:
        return True
    return False


def credential_gap(provider: str, endpoint: Optional[dict] = None) -> Optional[dict]:
    """Return a structured description of missing credentials, or None if OK.

    Resolution order matches execution: an ``endpoint`` (e.g. a capability model's
    own ``base_url``/``api_key``) overrides the provider's env credentials. A
    provider is usable when a *real-looking* api_key is present (from endpoint or
    env) — a placeholder key (non-empty but matching ``looks_like_placeholder``)
    counts as a gap, so a switch to such a model is attributed up-front instead
    of surfacing as a bare 401 on the next call. The returned dict NEVER contains
    key values — only the provider name and which fields are missing.

    ``anthropic`` and other providers without a base_url_env legitimately have no
    base_url; only a missing api_key counts as a gap there.
    """
    endpoint = endpoint or {}
    creds = get_provider_credentials(provider)
    api_key = (endpoint.get("api_key") or creds.get("api_key") or "").strip()
    has_base_url_env = bool(PROVIDER_CONFIG.get(provider, {}).get("base_url_env"))
    base_url = (endpoint.get("base_url") or creds.get("base_url") or "").strip()

    missing = []
    if not api_key:
        missing.append("api_key")
    elif looks_like_placeholder(api_key):
        missing.append("api_key_placeholder")
    # Only flag base_url when the provider declares one (env-driven) and neither
    # the endpoint nor env supplied it.
    if has_base_url_env and not base_url:
        missing.append("base_url")

    if not missing:
        return None
    return {"provider": provider, "missing": missing}


LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "")
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_BASE_URL = os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")