## ADDED Requirements

### Requirement: Original goal is anchored to user evidence
The planner memory view SHALL retain a goal anchor derived from the first substantive user request, including source reference and transaction time.

#### Scenario: New planner session
- **WHEN** a user submits the first substantive creation request
- **THEN** the system stores a goal anchor before applying any model-authored memory update

#### Scenario: Legacy session without anchor
- **WHEN** an existing session is loaded without goal-anchor metadata
- **THEN** the system derives the anchor from the persisted original user request or earliest substantive user message

### Requirement: Memory updates are governed candidates
Model-authored memory updates MUST be validated against the goal anchor, recent user evidence, field policy, and current proposal state before changing the materialized planner-memory view.

#### Scenario: Additive refinement
- **WHEN** a model update adds constraints or preferences supported by recent user messages
- **THEN** the normalized values merge without deleting prior supported facts

#### Scenario: Unrelated summary overwrite
- **WHEN** a model attempts to replace a stock-selection goal with an unrelated customer-service goal without user evidence
- **THEN** the replacement is rejected and the original requirement summary remains unchanged

#### Scenario: Explicit user pivot
- **WHEN** the user explicitly changes the requested Agent purpose in a recent message
- **THEN** the system accepts a replacement summary and records the user evidence that authorized it

### Requirement: Memory decisions are auditable
Every accepted or rejected high-impact planner-memory update SHALL record field, candidate summary, decision, reason, source turn, and transaction time without storing credentials.

#### Scenario: Rejected conflicting update
- **WHEN** a conflicting requirement summary is rejected
- **THEN** the audit view records the rejection reason and source turn while the materialized view remains unchanged

### Requirement: Readiness cannot be hallucinated
Model-authored memory MUST NOT set apply readiness to ready unless a validated proposal exists.

#### Scenario: Ready update without proposal
- **WHEN** a memory update requests `apply_readiness.status=ready` before proposal validation
- **THEN** the policy keeps readiness not-ready and records the rejected transition

### Requirement: Memory remains conversation-isolated
Planner memory anchors, candidates, audit records, decisions, and materialized views MUST be scoped to the current conversation and tenant/workspace boundary.

#### Scenario: Concurrent planner sessions
- **WHEN** two sessions submit overlapping or conflicting memory updates
- **THEN** each session recalls and persists only its own goal anchor and governed updates
