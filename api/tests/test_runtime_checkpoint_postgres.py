"""Integration gate for the production LangGraph PostgreSQL checkpointer."""

from __future__ import annotations

import asyncio
import os

from app.runtime_checkpoint import open_postgres_checkpointer, setup_postgres_checkpoints
from app.runtime_contract import RuntimeIdentity, RuntimeSource
from app.runtime_graph import AtlasAgentState, RuntimePhaseOneGraph


def test_langgraph_persists_checkpoint_to_postgres() -> None:
    if not os.getenv("RUN_POSTGRES_RUNTIME_TESTS"):
        return

    async def exercise() -> None:
        url = os.environ["LANGGRAPH_CHECKPOINT_DATABASE_URL"]
        await setup_postgres_checkpoints(url)
        identity = RuntimeIdentity(
            run_id="checkpoint-acceptance-run",
            thread_id="checkpoint-acceptance-thread",
            workspace_id="checkpoint-acceptance-workspace",
            user_id="checkpoint-acceptance-user",
            agent_id="checkpoint-acceptance-agent",
            agent_version_id="v1",
            source=RuntimeSource.API,
        )
        state = AtlasAgentState.initial(identity, "验证 PostgreSQL checkpoint")

        async with open_postgres_checkpointer(url) as checkpointer:
            graph = RuntimePhaseOneGraph(lambda _state: "checkpoint durable", checkpointer=checkpointer)
            result = await graph.ainvoke(state)
            persisted = await checkpointer.aget_tuple({"configurable": {"thread_id": identity.thread_id}})

        assert result.state.output == "checkpoint durable"
        assert persisted is not None

    asyncio.run(exercise())
