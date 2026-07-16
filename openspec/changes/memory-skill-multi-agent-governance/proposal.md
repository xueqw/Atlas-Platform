## Why

Atlas has useful demo-level memories, Skills, and a sequential runtime, but it lacks the governed, inspectable foundations required for durable memory, safe Skill selection, and dynamic multi-agent work. The supplied Atlas Memory + Skill + Multi-Agent specification defines those foundations and their acceptance boundary.

## What Changes

- Add an append-only memory ledger, bitemporal facts, deterministic tombstones, retrieval explanations, and checkpoint-ready state boundaries.
- Add Skill tree metadata, progressive disclosure, policy-filtered manual/automatic union, hybrid decision records, and no-selection guardrails.
- Add versioned worker contracts, envelope-based execution, permission intersection, authorization tickets, bounded DAG scheduling, and candidate-only asset creation.
- Preserve the existing production data-plane as a dependency: PostgreSQL/pgvector, Redis, MinIO, and Docker must be exercised in deployment verification before calling this production-ready.

## Capabilities

### New Capabilities
- `memory-governance`: Ledger-Views-Policy memory lifecycle and bitemporal isolation.
- `skill-discovery`: Hierarchical progressive Skill discovery and explainable decisions.
- `multi-agent-orchestration`: Bounded ephemeral workers, envelopes, tool authorization, and DAG coordination.

### Modified Capabilities
- `agent-memory`: Existing memory APIs become views over governed durable records rather than mutable standalone rows.
- `production-data-plane`: Production acceptance includes real pgvector, Redis, MinIO, and Docker exercise for the new records.

## Impact

Backend models, memory and chat integration, Skill APIs and planner inputs, runtime workflow execution, policy/audit records, tests, and deployment verification scripts.
