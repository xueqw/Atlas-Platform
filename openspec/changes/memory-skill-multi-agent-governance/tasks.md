## 1. Contracts and persistence

- [ ] 1.1 Add governed ledger, bitemporal fact, episodic/procedural candidate, router decision, worker, envelope, authorization, artifact, and audit schemas.
- [ ] 1.2 Add additive schema initialization and PostgreSQL/pgvector compatibility tests.
- [ ] 1.3 Define versioned JSON schemas for WorkerSpec, TaskEnvelope, ResultEnvelope, and ToolAuthorizationTicket.

## 2. Memory

- [ ] 2.1 Implement idempotent append-only ledger writes and tombstone deletion.
- [ ] 2.2 Implement scoped bitemporal fact queries, explanations, expiration, and untrusted-context formatting.
- [ ] 2.3 Implement session/working-state checkpoint and rebuild primitives with optimistic version checks.
- [ ] 2.4 Implement episodic evidence and procedural candidate generation; require evaluation and approval before publish.

## 3. Skill discovery

- [ ] 3.1 Extend Skill metadata and validate category depth, schemas, permissions, version, and status.
- [ ] 3.2 Implement tree-first metadata APIs, progressive content disclosure, policy-filtered binding union, and router audit records.
- [ ] 3.3 Add hybrid retrieval/reranking, negative scenarios, low-confidence no-selection, offline evaluation fixtures, and search/filter/sort pagination.

## 4. Multi-agent orchestration

- [ ] 4.1 Implement worker lifecycle constraints and isolated context/artifact envelopes.
- [ ] 4.2 Implement permission intersection, tool policy modes, tickets, parameter invalidation, scope, expiry, and anti-replay.
- [ ] 4.3 Implement dependency DAG dispatch, concurrency caps, timeout/retry/cancel propagation, replan cap, aggregation, and conflict checks.
- [ ] 4.4 Implement candidate Agent/Skill proposal, redaction, replay evaluation, human approval, versioned publish, rollback, and audit.

## 5. Integration and acceptance

- [ ] 5.1 Integrate governed memory and selected Skills into chat without exposing unselected Skill content.
- [ ] 5.2 Add unit/integration tests for cross-workspace/user/agent/worker isolation, time boundaries, tombstones, authorization replay, concurrent working-state edits, DAG parallelism, and candidate non-publication.
- [ ] 5.3 Run real PostgreSQL/pgvector, Redis, MinIO, Docker sandbox, backup/restore, and recovery verification; attach evidence.
- [ ] 5.4 Obtain an independent acceptance review against the supplied specification and resolve all blocking findings.
