## ADDED Requirements

### Requirement: Empty model responses fail visibly
The planner MUST NOT treat an HTTP-successful model response with zero usable content and no structured control result as a successful turn.

#### Scenario: Empty SSE response
- **WHEN** the selected provider closes an SSE response without emitting usable content
- **THEN** the planner retries once and emits an attributable failure if the retry is also empty

#### Scenario: Non-SSE JSON completion
- **WHEN** an OpenAI-compatible provider returns a valid JSON completion despite streaming being requested
- **THEN** the adapter extracts and processes the completion instead of emitting an empty success

### Requirement: Planner turn completion is content-safe
The planner SHALL persist an assistant message only when it contains visible text, a validated proposal, or a structured control result.

#### Scenario: Exhausted empty-response retry
- **WHEN** both the initial model call and the retry produce no usable result
- **THEN** no assistant message is persisted, the current stage is retained, active steps are finalized as failed, and the frontend receives a recoverable error

### Requirement: Confirmation continuation is deterministic
An advancing A2UI choice MUST be recorded idempotently and the subsequent planner continuation MUST observe that decision exactly once.

#### Scenario: Confirmation followed by automatic continuation
- **WHEN** a user selects an advancing confirmation option
- **THEN** the decision is durably recorded before the continuation turn generates the proposal

#### Scenario: Duplicate confirmation frame
- **WHEN** the same confirmation request and choice are submitted more than once
- **THEN** the system acknowledges the existing decision without duplicating ledger entries or planner turns

#### Scenario: Reconnect after confirmation
- **WHEN** the WebSocket reconnects after the decision is recorded but before proposal generation completes
- **THEN** the session restores the durable pending continuation and automatically resumes without returning to an unconfirmed state

#### Scenario: Disconnect before acknowledgement
- **WHEN** the decision is durably recorded but the A2UI acknowledgement is lost with the socket
- **THEN** reconnect exposes the same pending continuation token and the client safely replays it

#### Scenario: Disconnect after acknowledgement before continuation
- **WHEN** the client receives the acknowledgement but disconnects before sending the continuation message
- **THEN** reconnect exposes and automatically dispatches the pending continuation

#### Scenario: Duplicate continuation token
- **WHEN** the same continuation token reaches the backend more than once
- **THEN** the backend claims and consumes it once, runs exactly one planner turn, and acknowledges later copies as duplicates

### Requirement: Proposal stages are artifact-backed
The planner MUST NOT enter proposal confirmation or ready-to-apply state without the corresponding pending decision or validated proposal artifact.

#### Scenario: Text-only or empty completion
- **WHEN** a turn produces neither a pending confirmation request nor a validated proposal
- **THEN** session persistence does not mark it awaiting confirmation or ready to apply

### Requirement: Runtime evidence exposes recovery
The run-event stream SHALL expose empty-response retry, success, and terminal failure without leaking credentials or raw sensitive prompts.

#### Scenario: Retry succeeds
- **WHEN** the initial response is empty and the single retry returns usable content
- **THEN** the run timeline records the retry and completes the same logical turn successfully
