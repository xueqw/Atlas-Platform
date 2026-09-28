"""对指定知识库批量跑测试问题，打印命中文档、页码和检索分数。"""
import asyncio
import sys

from sqlalchemy import select
from app.database import SessionLocal
from app.models import KnowledgeBase
from app.knowledge import search_chunks
from app.model_gateway import embed_query

KB_NAME = sys.argv[1] if len(sys.argv) > 1 else "异构调度研究库"

QUERIES = [
    ("A 跨文献找方法", "哪些方法用强化学习做 DAG 任务调度"),
    ("A 跨文献找方法", "用 GNN 做任务卸载的工作"),
    ("A 跨文献找方法", "扩散模型怎么解决组合优化 NPC 问题"),
    ("B 精确方法名", "HEFT 算法的核心思想是什么"),
    ("B 近邻区分", "PEFT 和 HEFT 的区别"),
    ("C 换种说法(同义)", "截止时间和预算约束下的工作流调度"),
    ("C 换种说法(同义)", "无人机辅助的边缘计算任务调度"),
    ("D 边界(库里没有)", "区块链共识算法 PoW 和 PoS 的对比"),
    ("D 边界(库里没有)", "今天北京的天气怎么样"),
]


async def main() -> None:
    with SessionLocal() as db:
        kb = db.scalars(select(KnowledgeBase).where(KnowledgeBase.name == KB_NAME)).first()
        if not kb:
            print(f"找不到知识库：{KB_NAME}"); return
        print(f"知识库：{kb.name}  ({kb.id})\n" + "=" * 70)
        for tag, q in QUERIES:
            qv = await embed_query(q)
            hits = search_chunks(db, kb.id, q, qv, limit=3)
            print(f"\n【{tag}】问：{q}")
            if not hits:
                print("   → 0 命中（没有达到检索条件）")
                continue
            for h in hits:
                doc = h["document"][:46]
                quote = h["quote"][:54].replace("\n", " ")
                print(f"   {h['score']:.3f}  {doc} p{h['page']}  | {quote}")


if __name__ == "__main__":
    asyncio.run(main())
