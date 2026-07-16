"""Knowledge base engine: document loading, chunking, embedding, and retrieval."""

import os
import uuid
import json
from pathlib import Path
from typing import List, Optional, Dict, AsyncIterator
from dataclasses import dataclass, field

import chromadb
from chromadb.config import Settings as ChromaSettings


DATA_DIR = Path(__file__).parent.parent.parent / "data"
KB_ROOT = DATA_DIR / "knowledge_bases"


@dataclass
class IndexProgress:
    stage: str = "idle"
    total_chunks: int = 0
    completed_chunks: int = 0
    error: Optional[str] = None


_progress_store: Dict[str, IndexProgress] = {}


class Document:
    def __init__(self, filename: str, content: str):
        self.filename = filename
        self.content = content


class TextSplitter:
    def __init__(self, chunk_size: int = 500, chunk_overlap: int = 50):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def split(self, text: str) -> List[str]:
        chunks = []
        start = 0
        while start < len(text):
            end = start + self.chunk_size
            chunk = text[start:end]
            chunks.append(chunk)
            start = end - self.chunk_overlap
        return chunks


def _load_txt(path: Path) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _load_md(path: Path) -> str:
    return _load_txt(path)


def _load_pdf(path: Path) -> str:
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except ImportError:
        raise RuntimeError("PyPDF2 is required for PDF support. Run: pip install PyPDF2")


LOADERS = {
    ".txt": _load_txt,
    ".md": _load_md,
    ".pdf": _load_pdf,
}


def load_document(filepath: Path) -> Document:
    ext = filepath.suffix.lower()
    loader = LOADERS.get(ext)
    if not loader:
        raise ValueError(f"Unsupported file type: {ext}")
    content = loader(filepath)
    return Document(filename=filepath.name, content=content)


def _get_embedding_fn():
    global _embedding_fn
    if _embedding_fn is not None:
        return _embedding_fn
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("all-MiniLM-L6-v2", local_files_only=True)

    class EmbeddingFn:
        def __call__(self, input: List[str]) -> List[List[float]]:
            embeddings = model.encode(input, show_progress_bar=False)
            return embeddings.tolist()

    _embedding_fn = EmbeddingFn()
    return _embedding_fn


_embedding_fn = None


def _get_chroma_collection(agent_id: int):
    global _chroma_client
    if _chroma_client is None:
        _chroma_client = chromadb.PersistentClient(
            path=str(KB_ROOT),
            settings=ChromaSettings(
                anonymized_telemetry=False,
                is_persistent=True,
            ),
        )
    client = _chroma_client
    collection_name = f"agent_{agent_id}_kb"
    try:
        collection = client.get_collection(collection_name)
    except Exception:
        collection = client.create_collection(
            name=collection_name,
            embedding_function=_get_embedding_fn(),
        )
    return client, collection


_chroma_client = None


def get_index_progress(agent_id: int) -> IndexProgress:
    return _progress_store.get(str(agent_id), IndexProgress())


async def index_documents(
    agent_id: int,
    filepaths: List[Path],
) -> dict:
    progress = IndexProgress(stage="loading")
    _progress_store[str(agent_id)] = progress

    try:
        documents = []
        for fp in filepaths:
            documents.append(load_document(fp))

        splitter = TextSplitter(chunk_size=500, chunk_overlap=50)
        all_chunks = []
        chunk_sources = []
        for doc in documents:
            chunks = splitter.split(doc.content)
            all_chunks.extend(chunks)
            chunk_sources.extend([doc.filename] * len(chunks))

        progress.stage = "embedding"
        progress.total_chunks = len(all_chunks)

        client, collection = _get_chroma_collection(agent_id)

        batch_size = 20
        for i in range(0, len(all_chunks), batch_size):
            batch_chunks = all_chunks[i:i + batch_size]
            batch_sources = chunk_sources[i:i + batch_size]
            ids = [str(uuid.uuid4()) for _ in batch_chunks]
            metadatas = [{"source": s, "chunk_index": i + j} for j, s in enumerate(batch_sources)]

            collection.add(
                ids=ids,
                documents=batch_chunks,
                metadatas=metadatas,
            )
            progress.completed_chunks = min(i + batch_size, len(all_chunks))

        progress.stage = "complete"
        _progress_store[str(agent_id)] = progress

        return {
            "document_count": len(documents),
            "chunk_count": len(all_chunks),
            "status": "complete",
        }
    except Exception as e:
        progress.stage = "error"
        progress.error = str(e)
        _progress_store[str(agent_id)] = progress
        raise


def retrieve(agent_id: int, query: str, top_k: int = 3) -> List[dict]:
    _, collection = _get_chroma_collection(agent_id)

    results = collection.query(
        query_texts=[query],
        n_results=top_k,
    )

    items = []
    if results["ids"] and results["ids"][0]:
        for i in range(len(results["ids"][0])):
            items.append({
                "chunk_id": results["ids"][0][i],
                "content": results["documents"][0][i] if results["documents"] else "",
                "score": results["distances"][0][i] if results["distances"] else 0,
                "source": results["metadatas"][0][i].get("source", "") if results["metadatas"] and results["metadatas"][0] else "",
                "chunk_index": results["metadatas"][0][i].get("chunk_index", 0) if results["metadatas"] and results["metadatas"][0] else 0,
            })
    return items


def list_documents(agent_id: int) -> List[dict]:
    _, collection = _get_chroma_collection(agent_id)
    try:
        result = collection.get()
        if not result["ids"]:
            return []
        source_map: Dict[str, int] = {}
        for meta in result["metadatas"]:
            src = meta.get("source", "unknown")
            source_map[src] = source_map.get(src, 0) + 1
        return [
            {"filename": src, "chunk_count": count, "status": "indexed"}
            for src, count in source_map.items()
        ]
    except Exception:
        return []


def delete_document(agent_id: int, filename: str) -> int:
    _, collection = _get_chroma_collection(agent_id)
    result = collection.get(where={"source": filename})
    if result["ids"]:
        collection.delete(ids=result["ids"])
        return len(result["ids"])
    return 0