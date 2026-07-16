import tempfile
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlmodel import Session

from app.core import knowledge_engine as ke
from app.core.database import get_session
from app.api.pipeline import update_node_status

router = APIRouter(prefix="/agents", tags=["knowledge"])


ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


class DocumentItem(BaseModel):
    filename: str
    chunk_count: int
    status: str


class RetrieveRequest(BaseModel):
    query: str


class RetrieveItem(BaseModel):
    chunk_id: str
    content: str
    score: float
    source: str
    chunk_index: int


class RetrieveResponse(BaseModel):
    results: List[RetrieveItem]


class IndexResponse(BaseModel):
    document_count: int
    chunk_count: int
    status: str


@router.post("/{agent_id}/knowledge-base/documents", response_model=IndexResponse)
async def upload_documents(
    agent_id: int,
    files: List[UploadFile] = File(...),
    session: Session = Depends(get_session),
):
    saved: List[Path] = []
    for f in files:
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}")
        content = await f.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"File too large: {f.filename}")
        tmp = Path(tempfile.gettempdir()) / f"kb_{agent_id}_{f.filename}"
        tmp.write_bytes(content)
        saved.append(tmp)

    update_node_status(session, agent_id, "k", "in_progress")

    try:
        result = await ke.index_documents(agent_id, saved)
        score = 0.5
        if result["chunk_count"] > 0:
            score = min(1.0, result["chunk_count"] / 50.0)
        update_node_status(session, agent_id, "k", "complete", quality_score=score)
        return result
    except Exception as e:
        update_node_status(session, agent_id, "k", "failed")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        for p in saved:
            p.unlink(missing_ok=True)


@router.get("/{agent_id}/knowledge-base/documents", response_model=List[DocumentItem])
async def list_kb_documents(agent_id: int):
    return ke.list_documents(agent_id)


@router.delete("/{agent_id}/knowledge-base/documents/{filename:path}")
async def delete_kb_document(agent_id: int, filename: str):
    removed = ke.delete_document(agent_id, filename)
    if removed == 0:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"deleted": filename, "chunks_removed": removed}


@router.post("/{agent_id}/knowledge-base/retrieve", response_model=RetrieveResponse)
async def retrieve_chunks(agent_id: int, body: RetrieveRequest):
    items = ke.retrieve(agent_id, body.query)
    return RetrieveResponse(results=[RetrieveItem(**item) for item in items])