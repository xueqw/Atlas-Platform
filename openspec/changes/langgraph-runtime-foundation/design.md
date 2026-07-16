## Context

The host platform already has PostgreSQL/pgvector, Redis, MinIO, model gateway adapters, governed memory/Skill primitives, workflow records, and draft/published Agent versions. Its current runtime is spread across `main.py`, `apps.py`, `strategy.py`, `model_gateway.py`, and `workflow.py`. The application-development workbench is additionally hosted as a separate iframe service.

This change uses LangGraph for execution state, routing, interrupt points, retry policies, and checkpoint recovery. Atlas remains authoritative for tenancy, policy, audit, memory facts, Skill governance, artifacts, and deployment lifecycle.

## Architecture

### Runtime boundary

`AgentRuntimeService` owns all new-runtime execution. Callers submit a `RuntimeStartRequest` with immutable identity fields, source, version selection, input, requested resources, and idempotency key. The service resolves an Atlas Graph Package, creates or reuses a `RuntimeRun`, streams only Atlas Runtime Events, and exposes resume/cancel/history APIs.

The initial graph is deliberately bounded:

```text
load_package -> load_memory -> route_skills -> plan -> react -> validate -> finalize
                                      |             |
                                      |             -> recover -> react | finalize
                                      -> direct_response -> validate -> finalize
```

Only direct responses and read-only tool calls are enabled in the first graph. The graph state is serializable and carries references rather than secrets or large payloads. Future Plan-and-Execute, Builder, and Multi-Agent graphs are separate templates registered through the same service.

### State and checkpointing

`AtlasAgentState` contains immutable run identity, input/messages, selected capabilities, plan, node status, tool proposals/results, artifact references, retry counters, errors, and terminal output. Identity, permission, and version fields are never writable by model output.

Production uses a PostgreSQL LangGraph checkpointer. Local/unit tests may use an in-memory saver. Redis is limited to locks, short-lived cursors, rate limits, heartbeats, authorization tickets, and hot session copies. A Redis loss must not prevent a read-only run from being restored from PostgreSQL plus Atlas records.

`thread_id` is generated as `workspace_id:run_id`; a separate RuntimeRun row maps it to conversation, agent, and version. Resume and cancel acquire a run-scoped distributed lock.

### Events and projections

Every externally visible transition is converted to `atlas.runtime-event.v1` before leaving the service. A database transaction allocates a strictly monotonic sequence for one run and writes an immutable RuntimeEvent record. `stream(run_id, cursor)` replays stored events with sequence greater than the cursor and then subscribes to live events. Clients never parse LangGraph chunks.

WorkflowRun and WorkflowStep are projections. They may be rebuilt from events and cannot be used as the source of truth for resume.

### Error handling and safety

Errors are classified as transient, validation, permission, dependency, plan, side-effect, or terminal. Only transient/read-only failures are retried in Phase 1 with capped exponential backoff. Write tools are denied by the graph policy until Phase 2. Interrupt envelopes are JSON-only and bind an eventual authorization decision to run, actor, parameter digest, resource version, scope, expiry, and nonce. Side effects must execute in a node after `interrupt()` and be idempotent.

### Compatibility and rollout

`LANGGRAPH_RUNTIME_ENABLED` defaults to false. A Legacy Adapter wraps existing workbench behavior under the Runtime Event contract for comparison and safe fallback. A failure before any side effect may fall back only when the feature policy permits it; a failure after a side effect is terminal and never starts a duplicate legacy run. New database tables are additive.

## Delivery Standards

### Blocking acceptance

- A new-runtime Prompt Agent must start, checkpoint, emit ordered events, complete, and replay from a cursor.
- The same idempotency key must create one run under concurrent requests.
- Two workspaces with overlapping conversation/agent IDs must not read, resume, cancel, or stream each other's run/checkpoint/event state.
- Restarting the API and clearing Redis must not lose a paused/read-only checkpoint or duplicate an already completed node.
- Legacy runtime remains selectable with one feature flag and requires no database rollback.
- Frontend token/node/failure rendering uses Runtime Events, de-duplicates event IDs, and reconnects from the last sequence.

### Phase boundaries

Phase 2 adds write-tool interrupts and structured authorization cards. Phase 3 migrates Builder, draft preview, evaluation, published versions, and API. Phase 4 adds Plan-and-Execute plus governed memory/Skill views. Phase 5 adds bounded dynamic Multi-Agent subgraphs. No later phase may bypass this contract or the event ledger.

## Risks

- LangGraph checkpoint recovery is at-least-once around process failure; tool contracts and idempotency records are required before enabling writes.
- LLM output is not deterministic. Evaluation fixtures must pin model configuration and compare controlled policy/router paths and structured outputs, not claim identical freeform output.
- Checkpoints can grow quickly. Messages and large results must be summarized or stored as MinIO artifacts before production rollout.
- A PostgreSQL checkpointer package and migrations must be version-pinned in an ADR and executed by deployment, not request startup.
