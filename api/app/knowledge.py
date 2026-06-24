import io
import json
import math
import re
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
        raise ValueError("暂不支持该文件格式，请上传 PDF、DOCX、TXT、Markdown、CSV 或 JSON")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return [(1, raw.decode(encoding))]
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别文件编码")


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


def _terms(text: str) -> set[str]:
    lowered = text.lower()
    words = set(re.findall(r"[a-z0-9_]{2,}", lowered))
    chinese = [char for char in lowered if "\u4e00" <= char <= "\u9fff"]
    words.update(chinese)
    words.update("".join(chinese[i:i + 2]) for i in range(len(chinese) - 1))
    return words


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _format(rows: list[tuple[float, "DocumentChunk", "Document"]], limit: int) -> list[dict]:
    rows.sort(key=lambda item: item[0], reverse=True)
    return [{"document": document.name, "page": chunk.page, "quote": chunk.content[:260], "score": round(score, 3), "content": chunk.content} for score, chunk, document in rows[:limit]]


def search_chunks(db: Session, knowledge_base_id: str, query: str, query_vector: list[float] | None = None, limit: int = 4) -> list[dict]:
    """优先向量语义检索；无 query_vector（embedding 未配置/失败）时回退关键词重叠。"""
    rows = db.execute(
        select(DocumentChunk, Document)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.knowledge_base_id == knowledge_base_id, Document.status == "ready")
    ).all()

    if query_vector:
        ranked = []
        for chunk, document in rows:
            if not chunk.embedding:
                continue
            score = _cosine(query_vector, json.loads(chunk.embedding))
            if score >= settings.retrieval_min_score:
                ranked.append((score, chunk, document))
        # embedding 正常工作时信任它：没有过阈值的就是真的没相关资料，直接返回（空也返回），
        # 不再退关键词——否则会捞出一堆字面噪声（如"区块链"误匹配到"block"）。
        # 关键词只兜底 embedding 不可用（query_vector 为 None）的情况。
        return _format(ranked, limit)

    query_terms = _terms(query)
    ranked = []
    for chunk, document in rows:
        content_terms = _terms(chunk.content)
        score = len(query_terms & content_terms) / max(1, len(query_terms))
        if score > 0:
            ranked.append((score, chunk, document))
    return _format(ranked, limit)
