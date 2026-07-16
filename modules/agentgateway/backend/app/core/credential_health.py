"""Read-only credential health: are the configured API keys real, or placeholders?

The recurring failure mode (model 401, langfuse trace 404) is a credential that
is present in ``.env`` but is a placeholder — e.g. a 13-char ``sk-...`` stub left
over from ``.env.example``. The downstream symptom (401/404) gives no hint about
*which* key is unset. This module names the gap.

Everything here is READ-ONLY over environment variables: zero network, no side
effects, and — critically — the returned data NEVER contains a key VALUE, only
the provider name and a status. Safe to log and to surface to the frontend.
"""

from __future__ import annotations

import os
from typing import List, Optional, TypedDict

from app.core.config import (  # noqa: F401
    PROVIDER_CONFIG,
    LANGFUSE_BASE_URL,
    looks_like_placeholder as _looks_like_placeholder,
)


class CredentialStatus(TypedDict):
    name: str
    status: str  # "ok" | "missing" | "placeholder"


def _classify(value: Optional[str]) -> str:
    v = (value or "").strip()
    if not v:
        return "missing"
    if _looks_like_placeholder(v):
        return "placeholder"
    return "ok"


def scan_credentials() -> List[CredentialStatus]:
    """Return the health status of every known model provider + langfuse.

    Read-only over os.environ; never includes a key value. ``status`` is one of
    ``ok`` / ``missing`` / ``placeholder`` (``ok`` means "not an obvious
    placeholder", not a guarantee the key authenticates).
    """
    out: List[CredentialStatus] = []
    for provider, cfg in PROVIDER_CONFIG.items():
        env_name = cfg.get("api_key_env", "")
        out.append({"name": provider, "status": _classify(os.environ.get(env_name, ""))})
    # Langfuse uses two keys; report each so a placeholder secret (the observed
    # trace-404 cause) is visible even when the public key looks real.
    out.append({"name": "langfuse_secret", "status": _classify(os.environ.get("LANGFUSE_SECRET_KEY", ""))})
    out.append({"name": "langfuse_public", "status": _classify(os.environ.get("LANGFUSE_PUBLIC_KEY", ""))})
    return out


def unhealthy_summary() -> str:
    """One-line ``name=status`` summary of non-ok credentials, or "" if all ok.

    Suitable for a startup WARNING. Contains no key values.
    """
    bad = [c for c in scan_credentials() if c["status"] != "ok"]
    if not bad:
        return ""
    return ", ".join(f"{c['name']}={c['status']}" for c in bad)
