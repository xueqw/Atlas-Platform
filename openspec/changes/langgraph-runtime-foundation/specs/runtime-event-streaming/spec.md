## ADDED Requirements

### Requirement: Shared frontend runtime projection
The frontend SHALL reduce Runtime Events into a run-scoped projection containing status, plan, node state, tokens, sources, tools, errors, last sequence, and pending interrupt. The workbench and Agent Studio test path SHALL use this projection rather than independently assembling stream callbacks.

#### Scenario: run visibility
- **WHEN** a runtime emits node, token, retry, and terminal events
- **THEN** the workbench displays a coherent run state without reading LangGraph-internal chunks

### Requirement: reconnect behavior
The frontend SHALL retain the last observed sequence for an active run, reconnect with a cursor after a recoverable stream loss, and de-duplicate stable event IDs.

#### Scenario: stream interruption
- **WHEN** the event stream disconnects before a terminal event
- **THEN** the client attempts bounded reconnect, renders its connection state, and resumes from the last sequence without duplicate output
