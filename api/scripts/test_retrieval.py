"""Run representative questions against a knowledge base and print ranked hits."""

import asyncio
import sys

from sqlalchemy import select

from app.database import SessionLocal
from app.knowledge import search_chunks
from app.model_gateway import embed_query
from app.models import KnowledgeBase


KB_NAME = sys.argv[1] if len(sys.argv) > 1 else "Workflow Scheduling Research"
QUERIES = [
    ("method search", "Which papers use reinforcement learning for DAG scheduling?"),
    ("method search", "Which approaches use graph neural networks for task offloading?"),
    ("exact method", "What is the core idea behind the HEFT algorithm?"),
    ("near-neighbor", "What is the difference between PEFT and HEFT?"),
    ("paraphrase", "Workflow scheduling under deadline and budget constraints"),
    ("out of scope", "Compare proof-of-work and proof-of-stake consensus"),
    ("out of scope", "What is the weather in New York today?"),
]


async def main() -> None:
    with SessionLocal() as db:
        kb = db.scalars(select(KnowledgeBase).where(KnowledgeBase.name == KB_NAME)).first()
        if not kb:
            print(f"Knowledge base not found: {KB_NAME}")
            return
        print(f"Knowledge base: {kb.name} ({kb.id})\n" + "=" * 70)
        for category, question in QUERIES:
            query_vector = await embed_query(question)
            hits = search_chunks(db, kb.id, question, query_vector, limit=3)
            print(f"\n[{category}] {question}")
            if not hits:
                print("  No hits met the retrieval threshold.")
                continue
            for hit in hits:
                document = hit["document"][:46]
                quote = hit["quote"][:54].replace("\n", " ")
                print(f"  {hit['score']:.3f}  {document} p{hit['page']} | {quote}")


if __name__ == "__main__":
    asyncio.run(main())
