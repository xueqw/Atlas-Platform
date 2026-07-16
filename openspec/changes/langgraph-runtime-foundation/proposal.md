## Why

Atlas currently has separate execution paths for the workbench chat, Agent Studio preview, application development, evaluation, and published-agent APIs. They share models and some workflow records but not one durable runtime contract. This makes planning, memory, tool policy, retry, recovery, and product observability diverge by entry point.

The LangGraph migration plan defines the target architecture. This change delivers its first production-shaped slice: a unified, feature-flagged runtime foundation that can run low-risk Prompt Agents, persist a stable event stream, resume read-only work safely, and fall back to the legacy runtime without a database rollback.

## What Changes

- Introduce `AgentRuntimeService` as the sole new-runtime entry point with start, stream, resume, cancel, state, and history operations.
- Add a versioned Atlas runtime event contract with monotonic per-run sequence numbers, durable event records, and cursor-based replay.
- Add a LangGraph ReAct foundation with bounded planning, memory context, Skill routing, read-only tool dispatch, retry classification, and explicit terminal states.
- Add PostgreSQL-oriented checkpoint integration and a local test saver; Redis remains hot coordination only and is not an execution authority.
- Add an additive RuntimeRun/RuntimeEvent/RuntimeInterrupt persistence model and a WorkflowRun projection adapter.
- Add a Legacy Adapter and feature flags so existing entry points retain their current behavior until explicitly enabled.
- Add a reusable frontend runtime-event reducer and stream client for the workbench and Agent Studio test path. Application-development iframe integration is specified but remains a later migration task.

## Capabilities

### New Capabilities
- `langgraph-runtime-foundation`: Unified runtime contract, graph registry, LangGraph execution, checkpoint lifecycle, event ledger, cursor replay, and legacy fallback.
- `runtime-event-streaming`: Stable client event schema and frontend run-state reducer for streaming, reconnect, cancellation, and failure visibility.

### Modified Capabilities
- `production-runtime-architecture`: WorkflowRun and WorkflowStep become product projections of runtime events for the new path.
- `multi-agent-orchestration`: Existing worker envelopes remain domain-policy primitives; dynamic worker graphs are intentionally deferred until the foundation is accepted.

## Impact

Backend dependencies, SQLAlchemy models/schema initialization, model and tool adapters, workbench SSE endpoints, Agent Studio test flow, frontend API/types/state, tests, deployment configuration, and operational runbooks.

## Non-goals

- Do not cut all workbench, Builder, preview, evaluation, and external API traffic over in this change.
- Do not make Redis the durable checkpoint store.
- Do not replace Atlas Ledger, tenant policy, memory governance, Docker runner, or version lifecycle with LangGraph primitives.
- Do not enable non-idempotent external writes or dynamic multi-agent worker creation in the first runtime rollout.
