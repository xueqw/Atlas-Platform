## 1. Contracts and Routing

- [x] 1.1 Add strict execution strategy, classification, plan, review request/result, and revision request contracts with published JSON schemas.
- [x] 1.2 Implement the server-owned hybrid TaskStrategyRouter with mandatory safety floors, explicit overrides, reason codes, budgets, and deterministic tests.
- [x] 1.3 Extend Runtime request/state/handle persistence so the selected strategy and bounded complex-run progress survive restart and remain idempotency-bound.

## 2. Complex Runtime Graphs

- [x] 2.1 Implement a versioned Plan-Execute-Review runner with plan validation, sequential execution, independent reviewer creation, verdict handling, and bounded revise/replan loops.
- [x] 2.2 Integrate Multi-Agent execution with orchestrator-created isolated WorkerSpecs, dependency-aware parallel scheduling, strict envelopes, child RuntimeRuns, and existing authorization gates.
- [x] 2.3 Enforce Reviewer Subagent independence, read-only effective authority, complete acceptance coverage, fail-closed output validation, and structured escalation.
- [x] 2.4 Register ReAct, sequential PER, and Multi-Agent PER templates through a composite adaptive runner while preserving the Phase 1 alias and legacy rollback path.

## 3. Runtime Integration and Observability

- [x] 3.1 Expose `auto|react|plan-execute-review|multi-agent-plan-execute-review` at the Runtime API boundary and instantiate adaptive execution from the pinned AgentVersion snapshot.
- [x] 3.2 Emit and project bounded strategy, plan, worker, review, revision, replan, escalation, and terminal events without breaking unknown-event consumers.
- [x] 3.3 Verify tenant isolation, cancellation, resume/idempotency, Redis-loss recovery assumptions, artifact references, and no fallback after side effects.

## 4. Verification and Documentation

- [x] 4.1 Add unit and integration tests for routing thresholds, explicit overrides, all five review verdicts, invalid schemas, reviewer self-review/write denial, loop exhaustion, and concurrent workers.
- [x] 4.2 Run targeted and full API/web regression suites, strict OpenSpec validation, formatting checks, and production feature-flag checks.
- [x] 4.3 Update architecture, API, rollout, operations, and acceptance documentation with strategy semantics, schemas, event flow, budgets, and rollback guidance.
- [x] 4.4 Obtain an independent decoupled QA review against the OpenSpec delivery standard and resolve every blocking finding.
