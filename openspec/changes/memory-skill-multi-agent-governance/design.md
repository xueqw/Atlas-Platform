## Context

The platform already stores long memory and sequential workflow steps. This change introduces a separate governed model so existing chat APIs remain compatible while new flows have explicit identity, time, policy, and audit boundaries.

## Decisions

### Ledger before views

Every material memory or orchestration mutation is represented by a durable event with an idempotency key. Semantic, episodic, procedural, profile, session, working, and audit surfaces are views or checkpointed projections. Deletes append tombstones; they do not erase the audit path.

### Bitemporal facts

Semantic facts carry valid-time and transaction-time intervals, evidence, confidence, sensitivity, consent, embedding model version, and a supersession pointer. Queries require workspace, user, and agent scope by default and hide tombstoned or inactive versions.

### Skill disclosure is staged

The planner receives tree/category and compact metadata only. Candidate ranking uses supplied lexical/vector evidence and negative scenarios. Full content may be loaded only for policy-approved selected IDs. Every decision persists its inputs, scores, reasons, and result including no-selection.

### Workers are bounded contracts

The orchestrator is the sole worker factory. A versioned WorkerSpec defines scope, inputs/outputs, tools, resource limits, and lifecycle. Workers exchange Task/Result envelopes and artifact references, never shared mutable context. Default caps are six total workers, three concurrent workers, and two replans.

### Permissions are intersections

Effective tools equal user grant intersected with orchestrator grant, WorkerSpec, current authorization, and tool policy. High-risk actions use authorization tickets bound to normalized parameters; a parameter change invalidates the ticket.

### Production boundary

SQLite/local state can support deterministic unit tests only. Production completion requires compose or deployed verification against PostgreSQL with pgvector, Redis, MinIO, and Docker sandboxing, plus tenant-isolation and recovery evidence.

## Risks and Mitigations

- Projection drift: idempotent ledger consumers and replayable projections.
- Prompt injection through retrieved memory: mark memory as untrusted context and keep policy instructions separate.
- Worker privilege escalation: reject tool calls outside the permission intersection and log the denial.
- Runaway teams: enforce worker, concurrency, budget, replan, retry, and timeout limits at the orchestrator.

## Migration

New tables are additive. Legacy AgentMemory stays readable while migration adapters emit governed write events. Existing workflow steps remain compatible; multi-agent work records additional envelope/audit data. Do not remove legacy fields until backfill and rollback verification have completed.
