## Why

Atlas currently exposes a bounded ReAct runtime and separate Multi-Agent orchestration primitives, but it cannot yet choose an execution strategy from task characteristics or require an independent quality decision before a complex run succeeds. A unified adaptive runtime is needed so simple tasks stay fast while complex and decomposable tasks receive durable planning, isolated workers, independent review, and bounded recovery without bypassing tenant or tool policy.

## What Changes

- Add a server-owned execution-strategy router that selects `react`, `plan-execute-review`, or `multi-agent-plan-execute-review` from validated task signals, with an authorized explicit override and an auditable explanation.
- Register versioned LangGraph-compatible templates behind the existing Runtime service and feature flag rather than adding entry-point-specific agent loops.
- Add versioned Plan, Worker, Task, Result, Review Request, Review Result, and Revision Request contracts; every inter-agent boundary rejects unknown or cross-scope data.
- Add a bounded Plan → Execute → independent Reviewer → Finalize state machine supporting `PASS`, `REVISE`, `REPLAN`, `REJECT`, and `ESCALATE` decisions.
- Require the orchestrator to create isolated workers and a distinct ephemeral Reviewer Subagent. The reviewer is read-only, cannot review its own work, cannot dispatch workers, and cannot widen authority.
- Persist strategy decisions, plan versions, worker lifecycle, review evidence, iteration counts, and terminal reasons through Atlas Runtime Events and the existing PostgreSQL source of truth.
- Preserve the current write-tool confirmation gate, least-privilege tool intersections, Docker sandbox, Redis-loss recovery, legacy fallback rules, and default-off rollout.

## Capabilities

### New Capabilities

- `adaptive-execution-routing`: Server-owned classification, explicit overrides, strategy selection, budgets, observability, and safe fallback among supported runtime templates.
- `plan-execute-review-runtime`: Durable complex-task orchestration with versioned inter-agent schemas, isolated workers, an independent Reviewer Subagent, bounded revise/replan loops, and terminal decision handling.

### Modified Capabilities

None. Existing unarchived runtime and Multi-Agent changes remain compatible foundations; this change adds the next versioned layer without altering their published contract.

## Impact

- Runtime contracts, graph state/templates, graph registry, Runtime service, event projection, and API response schemas under `api/app`.
- Existing Multi-Agent worker/envelope/scheduler/service primitives and their persistence adapters.
- Runtime trace clients may render new strategy, plan, worker, and review events while remaining compatible with unknown event types.
- New deterministic and integration tests for routing, schema isolation, independent review, bounded recovery, persistence/resume, authorization, and legacy fallback.
- Runtime architecture and operations documentation; no new production service is required.
