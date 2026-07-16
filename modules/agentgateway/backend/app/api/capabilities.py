from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from app.core.database import get_session, engine
from app.models.db import CapabilityItem
from app.models.schemas import CapabilityItemCreate, CapabilityItemUpdate, CapabilityItemResponse
from app.core import skill_bundles

router = APIRouter(tags=["capabilities"])


class PlatformSkill(BaseModel):
    id: str
    name: str
    description: str = ""
    trigger_phrases: str = ""
    content: str = ""
    builtin: bool = False
    status: str = "active"


class PlatformConnector(BaseModel):
    provider: str
    name: str
    description: str = ""
    configured: bool = False
    connected: bool = False
    account_name: str = ""
    actions: list[str] = Field(default_factory=list)


class PlatformCapabilitySync(BaseModel):
    skills: list[PlatformSkill] = Field(default_factory=list)
    connectors: list[PlatformConnector] = Field(default_factory=list)


def _row_to_response(row: CapabilityItem) -> CapabilityItemResponse:
    return CapabilityItemResponse(
        id=row.id or 0,
        type=row.type,
        name=row.name,
        description=row.description,
        tags=row.tags,
        config=row.config,
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )


@router.get("/capabilities")
def list_capabilities(type: str | None = Query(None, description="Filter by type: prompt, model, tool")):
    with Session(engine) as session:
        stmt = select(CapabilityItem).order_by(CapabilityItem.updated_at.desc())
        if type:
            stmt = stmt.where(CapabilityItem.type == type)
        rows = session.exec(stmt).all()
        return [_row_to_response(r) for r in rows]


@router.get("/capabilities/search")
def search_capabilities(q: str = Query(..., min_length=1, description="Search keyword")):
    with Session(engine) as session:
        stmt = select(CapabilityItem).order_by(CapabilityItem.updated_at.desc())
        rows = session.exec(stmt).all()
        keyword = q.lower()
        results = [
            r for r in rows
            if keyword in r.name.lower() or keyword in r.description.lower() or keyword in r.tags.lower()
        ]
        return [_row_to_response(r) for r in results]


@router.post("/capabilities/platform-sync")
def sync_platform_capabilities(body: PlatformCapabilitySync):
    """Upsert the host platform's Skills and connectors into this workbench.

    The host stays the source of truth. We persist a compact, source-tagged copy
    so planners and DAG configuration can discover the same capabilities.
    """
    with Session(engine) as session:
        synced = {"skills": 0, "connectors": 0}

        def upsert(item_type: str, source_id: str, name: str, description: str, tags: list[str], config: dict):
            existing = session.exec(select(CapabilityItem)).all()
            for row in existing:
                try:
                    import json
                    row_config = json.loads(row.config or "{}")
                except (ValueError, TypeError):
                    row_config = {}
                if row_config.get("source") == "atlas_platform" and row_config.get("source_id") == source_id:
                    row.name = name
                    row.description = description
                    row.tags = json.dumps(tags, ensure_ascii=False)
                    row.config = json.dumps(config, ensure_ascii=False)
                    session.add(row)
                    return
            session.add(CapabilityItem(
                type=item_type,
                name=name,
                description=description,
                tags=__import__("json").dumps(tags, ensure_ascii=False),
                config=__import__("json").dumps(config, ensure_ascii=False),
            ))

        for skill in body.skills:
            upsert(
                "skill", f"skill:{skill.id}", skill.name, skill.description,
                ["atlas", "skill", *( ["builtin"] if skill.builtin else [] )],
                {
                    "source": "atlas_platform", "source_id": f"skill:{skill.id}",
                    "trigger_phrases": skill.trigger_phrases, "content": skill.content,
                    "builtin": skill.builtin, "status": skill.status,
                },
            )
            synced["skills"] += 1

        for connector in body.connectors:
            state = "connected" if connector.connected else "configured" if connector.configured else "not_configured"
            upsert(
                "tool", f"connector:{connector.provider}", connector.name,
                connector.description or f"Platform connector: {connector.name}",
                ["atlas", "connector", connector.provider, state],
                {
                    "source": "atlas_platform", "source_id": f"connector:{connector.provider}",
                    "provider": connector.provider, "connected": connector.connected,
                    "configured": connector.configured, "account_name": connector.account_name,
                    "actions": connector.actions,
                },
            )
            synced["connectors"] += 1

        session.commit()
        return {"ok": True, **synced}


# ── Skill bundle upload / content ────────────────────────────────────────────

_ZIP_MIMES = {"application/zip", "application/x-zip-compressed", "application/octet-stream"}


@router.post("/capabilities/skills/upload", status_code=201)
async def upload_skill_bundle(file: UploadFile = File(...)):
    """Accept a single ``.zip`` skill bundle, extract + archive its file tree to
    object storage, parse SKILL.md, and upsert a skill capability."""
    filename = file.filename or ""
    mime = (file.content_type or "").split(";")[0].strip().lower()
    if not filename.lower().endswith(".zip") and mime not in _ZIP_MIMES:
        raise HTTPException(
            status_code=415,
            detail={"error": "unsupported_media_type", "mime": mime, "filename": filename},
        )

    data = await file.read()
    if len(data) > skill_bundles.MAX_ZIP_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"error": "file_too_large", "limit": skill_bundles.MAX_ZIP_BYTES},
        )

    try:
        result = skill_bundles.store_and_register(data, original_filename=filename)
    except skill_bundles.SkillBundleError as e:
        raise HTTPException(status_code=400, detail={"error": "invalid_bundle", "message": str(e)})
    return result


@router.get("/capabilities/skills/{name}/content")
def get_skill_bundle_content(name: str):
    """Return a skill's SKILL.md body + file list."""
    body = skill_bundles.read_skill_md(name)
    if body is None:
        raise HTTPException(status_code=404, detail="skill bundle not found")
    files = skill_bundles.list_skill_files(name) or []
    return {"name": name, "skill_md": body, "files": files}


@router.get("/capabilities/skills/{name}/files/{path:path}")
def get_skill_bundle_file(name: str, path: str):
    """Return a single member file's bytes from the skill's extracted tree."""
    try:
        data = skill_bundles.read_skill_file(name, path)
    except skill_bundles.SkillBundleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if data is None:
        raise HTTPException(status_code=404, detail="file not found")
    import mimetypes

    media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return Response(content=data, media_type=media_type)


class ModelTestRequest(BaseModel):
    base_url: str
    api_key: str
    model_id: str


@router.post("/capabilities/test-model")
async def test_model_connection(body: ModelTestRequest):
    """Test if a model endpoint is reachable and responds correctly."""
    import httpx

    url = body.base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {body.api_key}",
    }
    payload = {
        "model": body.model_id,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
        "stream": False,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                content = ""
                if data.get("choices"):
                    content = data["choices"][0].get("message", {}).get("content", "")
                return {"success": True, "message": f"连接成功，模型响应: {content[:50]}"}
            else:
                return {"success": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except httpx.TimeoutException:
        return {"success": False, "message": "连接超时（15秒）"}
    except Exception as e:
        return {"success": False, "message": f"连接失败: {str(e)}"}


@router.post("/capabilities", status_code=201)
def create_capability(body: CapabilityItemCreate):
    with Session(engine) as session:
        item = CapabilityItem(
            type=body.type,
            name=body.name,
            description=body.description,
            tags=body.tags,
            config=body.config,
        )
        session.add(item)
        session.commit()
        session.refresh(item)
        return _row_to_response(item)


@router.put("/capabilities/{capability_id}")
def update_capability(capability_id: int, body: CapabilityItemUpdate):
    with Session(engine) as session:
        item = session.get(CapabilityItem, capability_id)
        if not item:
            raise HTTPException(status_code=404, detail="Not found")
        if body.type is not None:
            item.type = body.type
        if body.name is not None:
            item.name = body.name
        if body.description is not None:
            item.description = body.description
        if body.tags is not None:
            item.tags = body.tags
        if body.config is not None:
            item.config = body.config
        session.add(item)
        session.commit()
        session.refresh(item)
        return _row_to_response(item)


@router.delete("/capabilities/{capability_id}")
def delete_capability(capability_id: int):
    with Session(engine) as session:
        item = session.get(CapabilityItem, capability_id)
        if not item:
            raise HTTPException(status_code=404, detail="Not found")
        # For uploaded skills, sweep the archived extracted tree from object
        # storage so deleting a skill never leaks orphan objects.
        if item.type == "skill":
            import json

            try:
                cfg = json.loads(item.config or "{}")
            except (json.JSONDecodeError, TypeError):
                cfg = {}
            if isinstance(cfg, dict):
                skill_bundles.delete_skill_tree(cfg)
        session.delete(item)
        session.commit()
        return {"ok": True}
