## 1. Specification and technical validation

- [x] 1.1 Pin compatible LangGraph, LangChain Core, and PostgreSQL checkpointer dependencies; add an ADR and a real PostgreSQL Spike.
- [x] 1.2 Add RuntimeStart, RuntimeHandle, RuntimeResume, RuntimeState, and RuntimeEvent schemas plus contract tests.
- [x] 1.3 Add additive RuntimeRun, RuntimeEvent, RuntimeInterrupt persistence and ordered event allocation.
- [x] 1.4 Add PostgreSQL checkpointer setup/migration workflow; retain an in-memory saver for unit tests only.

## 2. Backend runtime foundation

- [x] 2.1 Implement AgentRuntimeService, graph registry, immutable state validation, idempotent start, get_state/history, cancel, and tenant checks.
- [x] 2.2 Implement minimal direct/read-only ReAct LangGraph template with model, memory, Skill, Tool Policy, retry classification, recovery, and terminal event adapters.
- [x] 2.3 Implement durable event replay and SSE cursor stream; project runtime events into existing WorkflowRun/WorkflowStep records.
- [x] 2.4 Implement Legacy Adapter and feature flags; prohibit fallback after any side effect.
- [x] 2.5 Add interrupt/resume persistence primitives and anti-replay validation; keep write execution disabled until Phase 2.

## 3. Frontend runtime projection

- [x] 3.1 Add versioned Runtime Event types and a run-scoped reducer with event-ID de-duplication and cursor tracking.
- [x] 3.2 Add a Runtime stream client with reconnect/backoff and migrate the host workbench chat to it under the feature flag.
- [x] 3.3 Migrate Agent Studio test to the shared projection and expose node/retry/failure/run status.
- [x] 3.4 Define iframe application-development bridge contract; defer the independent workbench UI migration to Phase 3.

## 4. Verification and release gates

- [x] 4.1 Add unit tests for state reducers, route selection, error classification, event ordering, tenant isolation, and idempotent start/resume.
- [x] 4.2 Add real PostgreSQL plus Redis integration tests for checkpoint recovery, Redis loss, cursor replay, and concurrent requests.
- [x] 4.3 Add frontend tests for event de-duplication, reconnect/cursor behavior, failure display, and feature fallback.
- [x] 4.4 Obtain an independent decoupled quality review; resolve all blocking findings before enabling the runtime flag for any user traffic.
