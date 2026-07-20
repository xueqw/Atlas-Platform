## ADDED Requirements

### Requirement: Server-owned strategy selection
The Runtime SHALL select exactly one versioned execution strategy from `react`, `plan-execute-review`, or `multi-agent-plan-execute-review` before task execution begins, and SHALL persist the decision, policy version, reason codes, score, and confidence.

#### Scenario: Simple task selects ReAct
- **WHEN** an auto-mode task has one objective, no dependency or multi-artifact requirement, no mandatory high-risk operation, and fits the configured ReAct budget
- **THEN** the Runtime selects `react` and emits one `strategy.selected` event before execution nodes run

#### Scenario: Decomposable task selects Multi-Agent PER
- **WHEN** an auto-mode task requires multiple independent workstreams or explicitly requests parallel specialist roles
- **THEN** the Runtime selects `multi-agent-plan-execute-review` and records the triggering reason codes

#### Scenario: Complex sequential task selects PER
- **WHEN** an auto-mode task requires a durable multi-step plan or reviewed artifact but cannot be safely decomposed
- **THEN** the Runtime selects `plan-execute-review`

### Requirement: Mandatory safety routing
The router SHALL be a trusted server component, SHALL validate structured classifier output, and SHALL prevent model output from downgrading a task that policy marks as requiring planning, review, authorization, or human escalation.

#### Scenario: Model proposes unsafe downgrade
- **WHEN** the classifier proposes ReAct for a task containing a mandatory complex or high-risk signal
- **THEN** the router ignores the downgrade and selects the minimum policy-required complex strategy

### Requirement: Explicit strategy override
The Runtime SHALL accept only `auto`, `react`, `plan-execute-review`, or `multi-agent-plan-execute-review` as an execution-strategy request and SHALL bind it to the idempotent start request without changing tenant identity, AgentVersion, or tool grants.

#### Scenario: Valid explicit override
- **WHEN** an authorized caller starts a run with an allowed explicit strategy
- **THEN** the Runtime validates the request, persists the chosen strategy, and uses the corresponding registered template

#### Scenario: Idempotency key reused with different strategy
- **WHEN** a caller reuses an idempotency key with a different execution strategy
- **THEN** the Runtime rejects the request as an idempotency conflict

### Requirement: Immutable strategy and bounded fallback
The chosen strategy SHALL remain immutable after execution starts. A template failure MAY fall back only when the configured legacy policy allows it and no side effect or worker write has started.

#### Scenario: Failure after a side effect boundary
- **WHEN** a selected strategy fails after a side effect starts
- **THEN** the Runtime fails or pauses the run without switching strategy or replaying the task through legacy execution

### Requirement: Strategy observability and compatibility
All strategies SHALL use the existing Runtime request, state, event, checkpoint, resume, cancel, and history boundaries. The adaptive feature SHALL remain default-off and existing event consumers SHALL tolerate new event types.

#### Scenario: Adaptive execution disabled
- **WHEN** the adaptive feature flag is false
- **THEN** existing ReAct/legacy behavior remains selectable without a database rollback

#### Scenario: Tenant attempts to inspect another route decision
- **WHEN** a user requests a run or event stream outside its workspace and actor scope
- **THEN** the Runtime returns the existing not-found/access-denied behavior without exposing the strategy decision
