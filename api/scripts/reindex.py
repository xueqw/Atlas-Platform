"""给已入库但没有向量的 chunk 补算 embedding。

用法（在 api/ 目录下）：
    python -m scripts.reindex
新加向量检索后，跑一次把历史文档补上索引；之后上传的文档会自动带向量。
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
            print("没有待补向量的 chunk，全部已建索引。")
            return
        print(f"待补 {len(pending)} 个 chunk，调用 embedding 中……")
        vectors = await embed_texts([chunk.content for chunk in pending])
        if not vectors:
            print("embedding 未返回结果（检查 SILICONFLOW_API_KEY 是否配置）。")
            return
        for chunk, vector in zip(pending, vectors):
            chunk.embedding = json.dumps(vector)
        db.commit()
        print(f"完成：{len(vectors)} 个 chunk 已写入向量。")


if __name__ == "__main__":
    asyncio.run(main())
