"""Backfill embeddings for stored chunks that do not have a vector.

Run from the api directory:
    python -m scripts.reindex
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from app.database import SessionLocal, ensure_schema
from app.models import DocumentChunk
from app.model_gateway import embed_texts


async def main() -> None:
    ensure_schema()
    with SessionLocal() as db:
        pending = db.scalars(
            select(DocumentChunk).where(DocumentChunk.embedding.is_(None))
        ).all()
        if not pending:
            print("All document chunks already have embeddings.")
            return
        print(f"Generating embeddings for {len(pending)} chunks...")
        vectors = await embed_texts([chunk.content for chunk in pending])
        if not vectors:
            print("No embeddings were returned. Check the OpenAI embedding configuration.")
            return
        for chunk, vector in zip(pending, vectors):
            chunk.embedding = json.dumps(vector)
        db.commit()
        print(f"Completed: stored {len(vectors)} embedding vectors.")


if __name__ == "__main__":
    asyncio.run(main())
