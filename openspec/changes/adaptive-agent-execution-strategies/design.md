## Context

Atlas has one durable Runtime service and event contract, a Phase 1 ReAct graph, PostgreSQL checkpointing, tenant-scoped run persistence, and a separately durable Multi-Agent DAG executor. The latter already creates bounded WorkerSpecs, validates Task/Result envelopes, runs isolated child RuntimeRuns, intersects tool grants, persists orchestration state, and supports retry/replan. What is missing is a server-owned strategy decision and a complex-task graph that makes independent review a prerequisite for success.

The implementation must preserve default-off rollout, immutable AgentVersion selection, PostgreSQL as recovery authority, Redis as rebuildable coordination state, the existing write confirmation ticket, Docker sandboxing, and legacy fallback only before side effects.

## Goals / Non-Goals

**Goals:**

- Select one of three versioned strategies: bounded ReAct, sequential Plan-Execute-Review, or Multi-Agent Plan-Execute-Review.
- Make the selection explainable, deterministic under test, overrideable only through a validated request field, and immutable after execution starts.
- Reuse the existing Runtime service, event ledger, checkpointing, worker runtime adapter, durable DAG scheduler, and tool policy.
- Require a separately instantiated Reviewer Subagent for every complex run.
- Enforce versioned JSON contracts at every orchestrator/worker/reviewer boundary.
- Bound worker count, concurrency, review rounds, revise rounds, and replans.
- Preserve restart, cancel, tenant isolation, and authorization behavior.

**Non-Goals:**

- Arbitrary model-authored Python, graph topology, tool definitions, or permission grants.
- Unbounded recursive teams or worker-to-worker direct messaging.
- Exposing chain-of-thought to the reviewer or clients.
- Automatically publishing learned Skill candidates from an unreviewed or failed run.
- Enabling the new runtime feature flag by default.

## Decisions

### 1. Strategy selection is a server-owned policy boundary

Add `ExecutionStrategy` and `StrategyDecision` contracts plus a `TaskStrategyRouter`. An explicit `auto|react|plan-execute-review|multi-agent-plan-execute-review` request is accepted only as a narrowing/selection hint; it never changes identity or grants. In `auto`, deterministic signals produce a score from decomposition, dependency, artifact, risk, tool, and requested-resource signals. An optional structured model classifier can add evidence but cannot lower mandatory safety rules. The router records reason codes, score, confidence, and policy version.

Alternative: let the first model response choose a graph. Rejected because model text is not a trustworthy authority boundary, is difficult to test, and can change after side effects.

### 2. Use a composite runner and versioned graph registry

Keep `RuntimePhaseOneGraph` unchanged as the simple strategy. Add a composite adaptive runner that routes once, writes the immutable decision into serializable state, and delegates to a registered template. Add registry keys for `react-v1`, `plan-execute-review-v1`, and `multi-agent-plan-execute-review-v1` while retaining `phase1-react` as an alias.

Alternative: embed every branch in the existing Phase 1 graph. Rejected because it creates one oversized graph, complicates rollback, and weakens per-template budgets.

### 3. Plan-Execute-Review is the complex-task control plane

The complex graph uses these logical nodes:

```text
classify -> plan -> validate_plan -> execute -> create_reviewer -> review -> decide
               ^                                            |           |
               |---------------- replan ---------------------|           +-> finalize
                              revise -> execute                           +-> reject/escalate
```

Sequential PER uses one isolated execution Worker. Multi-Agent PER uses the existing durable DAG with at most six workers and three concurrent workers. Both use the same independent reviewer contract and terminal policy. A review failure is not a pass. `REVISE` creates a scoped RevisionRequest for named tasks; `REPLAN` creates a new immutable plan version; `REJECT` fails the run; `ESCALATE` pauses the run for a user decision; `PASS` alone can finalize success.

### 4. Reviewer is a distinct ephemeral subagent

The orchestrator creates a new Reviewer WorkerSpec after execution results exist. Reviewer input contains the goal, acceptance criteria, plan version, sanitized ResultEnvelopes, artifact references, deterministic validation findings, and review rubric. It does not inherit executor messages or hidden reasoning. Its allowed tool set is a read-only intersection and it cannot dispatch workers, mutate results, approve tool calls, or review a result whose worker ID equals its own.

Reviewer output is `ReviewResultEnvelope.v1` with `PASS|REVISE|REPLAN|REJECT|ESCALATE`, per-criterion findings, evidence references, required actions, confidence, and scope identity. A deterministic validator runs before accepting it.

Alternative: have the orchestrator self-review. Rejected because it violates the requested independence and makes execution and acceptance share the same failure mode.

### 5. All inter-agent communication uses strict versioned schemas

Extend the existing WorkerSpec/TaskEnvelope/ResultEnvelope family with `PlanEnvelope`, `ReviewRequestEnvelope`, `ReviewResultEnvelope`, and `RevisionRequestEnvelope`. Unknown fields fail closed. Every envelope includes run/workspace/user/agent identity, sender/receiver, schema version, and artifact references. Large content stays in MinIO. Stored envelopes are detached from mutable worker objects.

### 6. Persist decisions as state plus Runtime Events

The Atlas graph state gains strategy, strategy decision, current plan version, review round, revision round, and final review fields. Node transitions emit bounded events such as `strategy.selected`, `plan.created`, `worker.created`, `worker.completed`, `review.requested`, `review.completed`, `review.revision_requested`, `review.replan_requested`, and `run.escalated`. Product Workflow rows remain projections only.

### 7. Failure and fallback remain side-effect aware

Classification or planning failure before a side effect may use the configured legacy fallback. Once workers or tools start a side effect, no strategy switch or legacy replay is permitted. Transient reviewer failures retry within the review budget; malformed reviewer output fails closed. A bounded loop exhaustion terminates with a plan/review category instead of silently finalizing partial results.

## Risks / Trade-offs

- [Router sends a borderline task to the expensive path] → Bias mandatory rules toward safety, expose reason codes, and track route distribution/cost.
- [Router sends a complex task to ReAct] → Treat multi-artifact, multi-domain, explicit decomposition, write/high-risk, and dependency signals as mandatory complex rules; permit explicit authorized override.
- [Reviewer repeats executor bias] → Use a separate prompt/context and allow a separately configured model; always combine it with deterministic schema/evidence checks.
- [Nested RuntimeRuns increase storage and latency] → Bound workers/rounds, store large bodies as artifacts, and preserve parent/child correlation IDs.
- [Crash between worker completion and review persistence] → Reuse stable envelope IDs and idempotency keys; reconstruct from PostgreSQL and never re-run accepted results.
- [Review loops never converge] → Cap revise and replan rounds at two each and surface a terminal/escalated reason.
- [Old clients do not understand new events] → Keep the existing event envelope and ensure clients ignore unknown types safely.

## Migration Plan

1. Add contracts/router/templates and deterministic tests with feature flags off.
2. Add persistence/events and integrate the existing durable orchestrator and subagent adapter.
3. Enable only in test/preview with shadow routing that records the decision while continuing ReAct.
4. Compare route correctness, cost, latency, review outcomes, tenant isolation, resume, and authorization behavior.
5. Enable adaptive execution per workspace/AgentVersion; keep explicit `react` and legacy fallback rollback paths.
6. Roll back by disabling the adaptive flag. Additive state/events remain readable and require no destructive migration.

## Open Questions

- Production thresholds and model-specific token budgets remain deploy policy, not hard-coded product behavior.
- High-risk workflows may later require two independent reviewers or a human approval quorum; v1 uses one reviewer plus deterministic checks.
