from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from typing import List
import json
import os

from app.core.database import get_session
from app.core.config import credential_gap
from app.core.model_caps import resolve_model_endpoint
from app.models.db import CapabilityItem
from app.models.schemas import ModelRegistryResponse

router = APIRouter(prefix="/models", tags=["models"])


def _capability_to_model(item: CapabilityItem) -> ModelRegistryResponse | None:
    """Project a capability-library model item into the model list shape.

    Models are single-sourced in ``capability_items(type=model)`` (see
    unify-model-source-to-capability), so the item's own positive ``id`` is used
    directly — no synthetic negative id is needed (there is no registry id to
    collide with). The config's ``api_key`` / ``base_url`` are deliberately NOT
    exposed — only the registry-shaped fields.
    """
    try:
        cfg = json.loads(item.config or "{}")
        cfg = cfg if isinstance(cfg, dict) else {}
    except (json.JSONDecodeError, TypeError):
        cfg = {}
    model_id = str(cfg.get("model_id") or "").strip()
    if not model_id:
        return None
    return ModelRegistryResponse(
        id=item.id or 0,
        provider=str(cfg.get("provider") or "glm"),
        model_id=model_id,
        display_name=item.name or model_id,
        capability_tags=item.tags or "[]",
        context_window=int(cfg.get("context_window") or 8192),
        max_output_tokens=int(cfg.get("max_output_tokens") or 4096),
        input_price_per_1k=float(cfg.get("input_price_per_1k") or 0.0),
        output_price_per_1k=float(cfg.get("output_price_per_1k") or 0.0),
        supports_streaming=bool(cfg.get("supports_streaming", True)),
        supports_vision=bool(cfg.get("supports_vision", False)),
        is_available=bool(cfg.get("is_available", True)),
    )


def _with_configuration_status(
    model: ModelRegistryResponse,
    session: Session,
) -> ModelRegistryResponse:
    """Annotate a catalog model without exposing any credential values."""
    endpoint = resolve_model_endpoint(model.model_id, session=session)
    provider = str(endpoint.get("provider") or model.provider)
    configured = credential_gap(provider, endpoint) is None
    allowlist = {
        item.strip()
        for item in os.environ.get("AGENTGATEWAY_MODEL_ALLOWLIST", "").split(",")
        if item.strip()
    }
    if allowlist and model.model_id not in allowlist:
        configured = False
    return model.model_copy(
        update={"configured": configured}
    )


@router.get("", response_model=List[ModelRegistryResponse])
def list_models(session: Session = Depends(get_session)):
    """List all available models from the single source of truth.

    Models live only in ``capability_items(type=model)`` (the registry is no
    longer a parallel source). Deduplicated by ``model_id`` (first occurrence
    wins) so a duplicate capability entry can't surface the same model twice.
    Only models whose config is not explicitly ``is_available=false`` are shown.
    """
    caps = session.exec(
        select(CapabilityItem).where(CapabilityItem.type == "model")
    ).all()
    out: List[ModelRegistryResponse] = []
    seen: set = set()
    for cap in caps:
        projected = _capability_to_model(cap)
        if projected is None or not projected.is_available:
            continue
        if projected.model_id in seen:
            continue
        seen.add(projected.model_id)
        out.append(_with_configuration_status(projected, session))
    return out


@router.get("/{model_id}", response_model=ModelRegistryResponse)
def get_model(model_id: int, session: Session = Depends(get_session)):
    """Fetch one model by its capability item id (single source of truth)."""
    item = session.get(CapabilityItem, model_id)
    if not item or item.type != "model":
        raise HTTPException(status_code=404, detail="Model not found")
    projected = _capability_to_model(item)
    if projected is None:
        raise HTTPException(status_code=404, detail="Model not found")
    return _with_configuration_status(projected, session)
