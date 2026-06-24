"""
RAG knowledge base service — Chroma + sentence-transformers.
Zero external server dependency: Chroma runs embedded, model auto-downloads.
"""

import logging
from collections.abc import Sequence
from pathlib import Path

import chromadb
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

_DB_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "chroma"

_EMBED_MODEL_NAME = "all-MiniLM-L6-v2"   # 22 MB — fast CPU, decent Chinese support
# For Chinese-first demos swap to: "BAAI/bge-small-zh-v1.5"  (198 MB)

_embedder: SentenceTransformer | None = None
_client: "chromadb.PersistentClient | None" = None


def _get_embedder() -> SentenceTransformer:
    global _embedder
    if _embedder is None:
        logger.info("Loading embedding model %s …", _EMBED_MODEL_NAME)
        _embedder = SentenceTransformer(_EMBED_MODEL_NAME)
    return _embedder


def _get_client() -> chromadb.PersistentClient:
    global _client
    if _client is None:
        _DB_DIR.mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(path=str(_DB_DIR))
    return _client


def get_collection(name: str) -> chromadb.Collection:
    """Return (or create) a named Chroma collection."""
    return _get_client().get_or_create_collection(name)


# ── document ingest ──────────────────────────────────────────────

def index_documents(
    collection_name: str,
    documents: list[str],
    *,
    ids: list[str] | None = None,
    metadatas: list[dict] | None = None,
) -> None:
    embedder = _get_embedder()
    collection = get_collection(collection_name)
    embeddings = embedder.encode(documents, show_progress_bar=False).tolist()
    if ids is None:
        import uuid
        ids = [str(uuid.uuid4()) for _ in documents]
    collection.add(
        documents=documents,
        embeddings=embeddings,
        ids=ids,
        metadatas=metadatas,
    )
    logger.info("Indexed %d document(s) into collection %r", len(documents), collection_name)


# ── retrieval ────────────────────────────────────────────────────

def retrieve(
    collection_name: str,
    query: str,
    *,
    top_k: int = 5,
) -> list[dict]:
    """Return top_k hits as list of {id, content, metadata, distance}."""
    embedder = _get_embedder()
    collection = get_collection(collection_name)
    q_vec = embedder.encode([query], show_progress_bar=False).tolist()
    results = collection.query(query_embeddings=q_vec, n_results=top_k, include=["documents", "metadatas", "distances"])
    hits: list[dict] = []
    if not results["ids"] or not results["ids"][0]:
        return hits
    for i, doc_id in enumerate(results["ids"][0]):
        hits.append({
            "id": doc_id,
            "content": results["documents"][0][i],
            "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
            "distance": results["distances"][0][i],
        })
    return hits


def delete_collection(collection_name: str) -> None:
    _get_client().delete_collection(collection_name)
    logger.info("Deleted collection %r", collection_name)
