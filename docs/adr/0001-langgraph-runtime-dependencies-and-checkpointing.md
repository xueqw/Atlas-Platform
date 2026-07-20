# ADR 0001: LangGraph Runtime Dependencies and Checkpointing

- Status: Accepted for the migration foundation; production enablement remains gated
- Date: 2026-07-16

## Context

Atlas is migrating the workbench and Agent Studio execution paths to one durable runtime while retaining the legacy path as a disabled-by-default rollback target. Runtime state must survive an API process restart, remain isolated by workspace/user/agent/version, and expose ordered events to existing workflow projections.

## Decision

The API pins the first compatibility set in `api/requirements.txt`:

- `langchain-core==1.4.9`
- `langgraph==1.2.9`
- `langgraph-checkpoint-postgres==3.1.0`
- `psycopg[binary]==3.2.9`
- `redis==6.2.0`

LangGraph is the graph/state-machine layer. `AsyncPostgresSaver` is the only supported production checkpointer; in-memory execution is restricted to unit tests and the explicit legacy adapter. Redis remains the short-lived session/cache layer and is not the authoritative graph checkpoint store. Atlas RuntimeRun/RuntimeEvent records remain the stable product contract and audit source; LangGraph checkpoint tables are an execution detail.

Checkpoint schema setup is an explicit deployment migration through `setup_postgres_checkpoints()`, never an implicit API-startup side effect. Runtime traffic remains off unless `LANGGRAPH_RUNTIME_ENABLED` is enabled and a PostgreSQL checkpoint URL is configured.

## Compatibility spike and release evidence

The repository-level gate is:

```bash
cd api
RUN_POSTGRES_RUNTIME_TESTS=1 \
LANGGRAPH_CHECKPOINT_DATABASE_URL='postgresql://atlas:<password>@127.0.0.1:5432/atlas' \
python -m pytest -q tests/test_runtime_checkpoint_postgres.py
```

The test initializes the upstream checkpoint schema, executes the minimal graph, then reads the stored checkpoint by thread ID. A release operator must attach the successful command output and tested PostgreSQL image digest to the change evidence before checking OpenSpec task 1.1 or enabling user traffic. Unit-test success alone is not evidence for this gate.

## Consequences

- Atlas can upgrade LangGraph independently behind its own versioned runtime/event contracts.
- Database credentials must be provided by secret file or deployment environment and never committed.
- Redis loss must not destroy durable run state; recovery and concurrent-request behavior remain separate real-infrastructure acceptance gates.
- Dependency upgrades require repeating the PostgreSQL compatibility spike and contract suite.
