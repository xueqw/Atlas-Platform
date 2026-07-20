## ADDED Requirements

### Requirement: Versioned complex-task plan
The orchestrator SHALL produce a strict versioned PlanEnvelope containing the run scope, goal, acceptance criteria, immutable plan version, bounded WorkerSpecs, TaskEnvelopes, dependencies, budgets, and expected result schemas before execution.

#### Scenario: Invalid or cyclic plan
- **WHEN** a plan contains an unknown worker, duplicate task, cyclic dependency, excessive worker count, invalid schema, or authority outside the orchestrator grant
- **THEN** validation rejects the plan before any worker executes

### Requirement: Isolated schema-bound workers
The orchestrator SHALL be the only component allowed to create workers. Workers SHALL receive isolated context and strict TaskEnvelopes, SHALL return strict ResultEnvelopes, SHALL have per-worker tools and budgets, and SHALL NOT communicate directly or share mutable state.

#### Scenario: Independent tasks execute concurrently
- **WHEN** a valid Multi-Agent plan has independent ready tasks
- **THEN** the scheduler executes them concurrently up to the configured cap and records their child RuntimeRun identifiers

#### Scenario: Cross-scope result is submitted
- **WHEN** a worker result changes the run, workspace, user, agent, task, or worker identity from its issued task
- **THEN** the orchestrator rejects the result and records a permission failure

### Requirement: Independent Reviewer Subagent
Every complex run SHALL create an ephemeral Reviewer Subagent after execution. The reviewer SHALL have an isolated prompt and context, read-only effective tools, a distinct worker identity, no dispatch or mutation authority, and SHALL receive only a validated ReviewRequestEnvelope.

#### Scenario: Executor attempts to review itself
- **WHEN** a review result is produced by an execution worker or uses the same identity as a reviewed worker
- **THEN** the orchestrator rejects it and the run cannot succeed

#### Scenario: Reviewer requests a write tool
- **WHEN** the reviewer proposes a write, publish, delete, authorization, or worker-dispatch operation
- **THEN** policy denies the operation without creating a confirmation ticket

### Requirement: Review verdict contract
The reviewer SHALL return a strict ReviewResultEnvelope with one verdict from `PASS`, `REVISE`, `REPLAN`, `REJECT`, or `ESCALATE`, per-criterion findings, evidence references, required actions, reviewed task IDs, and confidence. Deterministic checks SHALL run before accepting the verdict.

#### Scenario: Passing review
- **WHEN** all deterministic checks pass and the independent reviewer returns a valid `PASS` covering every acceptance criterion and task
- **THEN** the orchestrator may finalize the run as succeeded and persist the review evidence

#### Scenario: Malformed or incomplete review
- **WHEN** the reviewer output is invalid, omits required criteria/tasks, lacks evidence required by policy, or contains unknown fields
- **THEN** the review fails closed and the run does not enter a succeeded state

### Requirement: Bounded verdict handling
The orchestrator SHALL map verdicts deterministically: `PASS` to finalize, `REVISE` to scoped task revision, `REPLAN` to a new plan version, `REJECT` to failed, and `ESCALATE` to paused pending an authorized user decision. Revise and replan loops SHALL be bounded.

#### Scenario: Local revision requested
- **WHEN** a valid review returns `REVISE` with named tasks and required actions within the revision budget
- **THEN** the orchestrator issues versioned RevisionRequestEnvelopes only to those workers and reviews the new results again

#### Scenario: Replan requested
- **WHEN** a valid review returns `REPLAN` within the replan budget
- **THEN** the orchestrator creates and validates a new immutable plan version without mutating prior plan or result records

#### Scenario: Review budget exhausted
- **WHEN** another revise or replan would exceed the configured bound
- **THEN** the Runtime terminates with a review/plan failure or escalates according to policy and SHALL NOT silently accept partial results

#### Scenario: Human escalation
- **WHEN** the reviewer returns `ESCALATE`
- **THEN** the run persists a structured escalation reason and pauses until an actor-bound resume decision is supplied

### Requirement: Durable recovery and audit
Strategy, plans, worker lifecycle, Task/Result envelopes, review requests/results, iteration counters, and terminal decisions SHALL be durable and tenant scoped. Restart or Redis loss SHALL recover from PostgreSQL without rerunning accepted results or duplicating non-idempotent side effects.

#### Scenario: Restart after workers complete before review
- **WHEN** the process stops after ResultEnvelopes are persisted but before a ReviewResultEnvelope is accepted
- **THEN** resume reconstructs the review request from persisted records and does not re-execute completed workers

### Requirement: Governed tool and artifact access
Worker and reviewer authority SHALL be the intersection of user, orchestrator, AgentVersion, WorkerSpec, and current authorization grants. Write tools SHALL continue through the existing confirmation ticket, while reviewer tools remain read-only. Large payloads SHALL use tenant-bound artifact references.

#### Scenario: Worker requires a write tool
- **WHEN** an execution worker requests an allowed write operation without a valid actor-bound ticket
- **THEN** the worker pauses through the existing confirmation gate and cannot continue until the exact call is approved

#### Scenario: Artifact belongs to another tenant
- **WHEN** any worker or reviewer supplies an artifact reference outside the run workspace scope
- **THEN** the Runtime rejects access without revealing the object
