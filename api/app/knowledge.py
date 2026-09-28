import io
import json
import math
import re
from collections import Counter
from pathlib import Path
from docx import Document as DocxDocument
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session
from .config import settings
from .models import Document, DocumentChunk


def extract_pages(filename: str, raw: bytes) -> list[tuple[int, str]]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return [(index + 1, page.extract_text() or "") for index, page in enumerate(PdfReader(io.BytesIO(raw)).pages)]
    if suffix == ".docx":
        text = "\n".join(paragraph.text for paragraph in DocxDocument(io.BytesIO(raw)).paragraphs)
        return [(1, text)]
    if suffix not in {".txt", ".md", ".csv", ".json"}:
        raise ValueError("Unsupported file type. Upload a PDF, DOCX, TXT, Markdown, CSV, or JSON file.")
    try:
        return [(1, raw.decode("utf-8-sig"))]
    except UnicodeDecodeError as exc:
        raise ValueError("The file must use UTF-8 encoding") from exc


def split_pages(pages: list[tuple[int, str]], size: int = 700, overlap: int = 100) -> list[tuple[int, str]]:
    chunks: list[tuple[int, str]] = []
    for page, text in pages:
        clean = re.sub(r"\n{3,}", "\n\n", text).strip()
        start = 0
        while start < len(clean):
            end = min(len(clean), start + size)
            piece = clean[start:end].strip()
            if piece:
                chunks.append((page, piece))
            if end == len(clean):
                break
            start = end - overlap
    return chunks


def _tokens(text: str) -> list[str]:
    """Tokenize English text without an external dependency."""
    return re.findall(r"[a-z0-9_]+", text.lower())


def _bm25_scores(query: str, documents: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    """Return Okapi BM25 scores for ``documents``.

    BM25 rewards rare terms while normalising term frequency and chunk length,
    which makes keyword fallback useful on knowledge bases with mixed document
    sizes. The implementation is intentionally dependency-free because it is a
    resilience path used when the embedding service is unavailable.
    """
    if not documents:
        return []
    query_terms = set(_tokens(query))
    if not query_terms:
        return [0.0] * len(documents)

    tokenized = [_tokens(document) for document in documents]
    term_counts = [Counter(tokens) for tokens in tokenized]
    average_length = sum(len(tokens) for tokens in tokenized) / len(tokenized)
    if average_length == 0:
        return [0.0] * len(documents)

    document_frequency = {
        term: sum(1 for counts in term_counts if term in counts)
        for term in query_terms
    }
    document_count = len(documents)
    scores: list[float] = []
    for tokens, counts in zip(tokenized, term_counts):
        length_normalisation = 1 - b + b * len(tokens) / average_length
        score = 0.0
        for term in query_terms:
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            frequency_in_documents = document_frequency[term]
            inverse_document_frequency = math.log(
                1 + (document_count - frequency_in_documents + 0.5) / (frequency_in_documents + 0.5)
            )
            score += inverse_document_frequency * (
                frequency * (k1 + 1) / (frequency + k1 * length_normalisation)
            )
        scores.append(score)
    return scores


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    score = dot / norm if norm else 0.0
    return score if math.isfinite(score) else 0.0


def _load_embedding(raw: str | None) -> list[float] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
        if not isinstance(value, list) or not value:
            return None
        vector = [float(item) for item in value]
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return vector if all(math.isfinite(item) for item in vector) else None


def _format(rows: list[tuple[float, "DocumentChunk", "Document"]], limit: int) -> list[dict]:
    rows.sort(key=lambda item: item[0], reverse=True)
    return [{"document": document.name, "page": chunk.page, "quote": chunk.content[:260], "score": round(score, 3), "content": chunk.content} for score, chunk, document in rows[:limit]]


def search_chunks(db: Session, knowledge_base_id: str, query: str, query_vector: list[float] | None = None, limit: int = 4) -> list[dict]:
    """Prefer semantic vectors and fall back to BM25 when they are unavailable."""
    rows = db.execute(
        select(DocumentChunk, Document)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.knowledge_base_id == knowledge_base_id, Document.status == "ready")
    ).all()

    if query_vector:
        ranked = []
        compatible_embeddings = 0
        for chunk, document in rows:
            embedding = _load_embedding(chunk.embedding)
            if embedding is None or len(embedding) != len(query_vector):
                continue
            compatible_embeddings += 1
            score = _cosine(query_vector, embedding)
            if score >= settings.retrieval_min_score:
                ranked.append((score, chunk, document))
        # Trust a compatible semantic index, including an empty result below
        # the relevance threshold. BM25 is only a resilience fallback.
        if compatible_embeddings:
            return _format(ranked, limit)
        # An incompatible stored index falls back to BM25 until reindexing.

    raw_scores = _bm25_scores(query, [chunk.content for chunk, _ in rows])
    ranked = [
        # Convert the unbounded BM25 value to a stable 0..1 display score while
        # preserving its ordering.
        (score / (score + 1), chunk, document)
        for score, (chunk, document) in zip(raw_scores, rows)
        if score > 0
    ]
    return _format(ranked, limit)
