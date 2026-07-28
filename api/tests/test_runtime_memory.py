from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.governance_models import GovernanceBase
from app.memory_ledger import MemoryScope, record_fact
from app.models import AgentMemory
from app.runtime_contract import RuntimeSource, RuntimeStartRequest
from app.runtime_graph import AtlasAgentState
from app.runtime_memory import RuntimeMemoryProvider


class HotStore:
    def __init__(self, messages):
        self.messages = messages
        self.keys = []

    def get(self, namespace, key):
        self.keys.append((namespace, key))
        return self.messages


def test_runtime_memory_provider_keeps_every_source_in_the_exact_tenant_scope():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    GovernanceBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    with factory() as db:
        db.add_all([
            AgentMemory(id="own", workspace_id="ws", user_id="user", agent_id="agent", category="preference", content="只给当前用户", expires_at=now + timedelta(days=1)),
            AgentMemory(id="other-user", workspace_id="ws", user_id="other", agent_id="agent", category="preference", content="不能泄漏", expires_at=now + timedelta(days=1)),
            AgentMemory(id="other-agent", workspace_id="ws", user_id="user", agent_id="other", category="preference", content="不能泄漏", expires_at=now + timedelta(days=1)),
        ])
        record_fact(
            db,
            scope=MemoryScope(workspace_id="ws", user_id="user", agent_id="agent"),
            subject="客户",
            predicate="偏好",
            object_value="中文",
            idempotency_key="own-fact",
        )
        record_fact(
            db,
            scope=MemoryScope(workspace_id="ws", user_id="other", agent_id="agent"),
            subject="客户",
            predicate="偏好",
            object_value="不能泄漏",
            idempotency_key="other-fact",
        )
        db.commit()

    request = RuntimeStartRequest(
        workspace_id="ws", user_id="user", agent_id="agent", agent_version_id="v1",
        source=RuntimeSource.WORKBENCH, input="你好", idempotency_key="memory-scope",
        conversation_id="conversation",
    )
    hot_store = HotStore([{"role": "assistant", "content": "短期上下文"}])
    context = __import__("asyncio").run(RuntimeMemoryProvider(factory, hot_store=hot_store).load(
        AtlasAgentState.initial(request.make_identity("run"), request.input),
    ))

    rendered = str(context)
    assert "只给当前用户" in rendered
    assert "短期上下文" in rendered
    assert "客户 偏好 中文" in rendered
    assert "不能泄漏" not in rendered
    assert hot_store.keys == [("short-memory", "ws:user:agent:conversation")]
