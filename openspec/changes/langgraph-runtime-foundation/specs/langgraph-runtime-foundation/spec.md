## ADDED Requirements

### Requirement: Unified runtime contract
The system SHALL expose an AgentRuntimeService with start, stream, resume, cancel, get_state, and get_history operations. A runtime start request SHALL include workspace, user, agent, source, version selection, input, and idempotency key. Identity and version fields SHALL be resolved server-side and SHALL NOT be mutable by model output.

#### Scenario: idempotent start
- **WHEN** the same caller submits the same idempotency key concurrently
- **THEN** the system returns one stable runtime handle and starts one graph execution

#### Scenario: tenant isolation
- **WHEN** a caller from another workspace requests a run, event stream, history, interrupt, or checkpoint
- **THEN** the system returns a non-disclosing authorization failure and emits no data

### Requirement: Durable runtime events
The system SHALL expose only `atlas.runtime-event.v1` events to product clients. Every event SHALL include an immutable event ID, run ID, strictly increasing sequence within the run, timestamp, type, and JSON-serializable payload. Runtime events SHALL be persisted before they are offered for replay.

#### Scenario: reconnect from cursor
- **WHEN** a client reconnects with the final observed sequence as cursor
- **THEN** it receives each later persisted event once and in sequence order

#### Scenario: duplicate delivery
- **WHEN** a client receives an already observed event ID after reconnecting
- **THEN** the frontend reducer does not duplicate rendered tokens, steps, or tool state

### Requirement: Checkpoint and Redis authority boundary
The system SHALL use a durable PostgreSQL checkpointer for production graph recovery. Redis SHALL only store explicitly non-authoritative hot coordination state. Checkpoint namespace and thread identity SHALL include workspace and run isolation.

#### Scenario: restore after API restart
- **WHEN** an in-progress read-only run is checkpointed, the API process restarts, and Redis hot state is cleared
- **THEN** the run can load its state/history from durable records without rerunning completed graph steps

### Requirement: Bounded initial graph and recovery
The first LangGraph template SHALL support direct response and read-only ReAct paths, bounded planning/tool iterations, classified transient retries, explicit terminal states, and event emission at node boundaries. Non-idempotent write tools SHALL be denied until the authorization graph is delivered.

#### Scenario: transient read-only failure
- **WHEN** a read-only model or tool call returns a classified transient failure
- **THEN** the runtime emits retry scheduling and bounded retry events before completing or failing with a classified reason

#### Scenario: write tool proposal
- **WHEN** the graph proposes a write-capable tool under the Phase 1 feature policy
- **THEN** the tool is not executed and the run emits an explicit policy denial

### Requirement: Legacy compatibility and rollout
The system SHALL preserve the legacy execution path behind a feature flag. Enabling or disabling the new runtime SHALL not require destructive schema rollback. A new-runtime failure SHALL not automatically invoke legacy execution after a side effect has occurred.

#### Scenario: feature rollback
- **WHEN** the LangGraph runtime feature flag is disabled
- **THEN** new requests use the legacy runtime while existing durable LangGraph runs remain inspectable and cannot execute new side effects
