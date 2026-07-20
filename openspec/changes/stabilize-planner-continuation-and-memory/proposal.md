## Why

AgentGateway currently treats a zero-token model stream as a successful planner turn, persists an empty assistant message, and can leave the UI in a misleading confirmation or drafting state without a proposal. The same flow also accepts ungrounded model-authored memory updates that can overwrite the user's original goal, as observed when a stock-selection Agent session was rewritten as a customer-service Agent.

## What Changes

- Detect empty, malformed, and non-SSE OpenAI-compatible responses and convert them into an attributable failure instead of a successful `done`.
- Retry one transient empty response and expose an explicit recoverable error when retry is exhausted.
- Make confirmation continuation deterministic: a recorded confirmation resumes the correct planner state and cannot create empty turns or silently remain in `awaiting_confirmation`.
- Require a validated proposal artifact before the session can enter proposal confirmation or ready-to-apply states.
- Add stable planner-memory invariants: preserve the original user goal, validate semantic updates against recent user evidence, reject unrelated overwrites, and retain provenance for accepted/rejected updates.
- Prevent empty assistant messages from being persisted or rendered as successful turns.
- Add regression, integration, reconnect, tenant/conversation isolation, and failure-recovery tests.

## Capabilities

### New Capabilities

- `planner-continuation-reliability`: Reliable model streaming, confirmation continuation, proposal-state transitions, retry, and user-visible failure recovery.
- `planner-memory-integrity`: Evidence-grounded planner memory updates, immutable goal anchoring, provenance, conflict rejection, and isolation guarantees.

### Modified Capabilities

None.

## Impact

- AgentGateway backend planner WebSocket, OpenAI-compatible streaming adapter, planner session persistence, proposal state transitions, memory update extraction/merge, and run-event emission.
- AgentGateway planner frontend reducer and progress presentation for empty/error/retry states.
- Planner and layered-memory test suites, including model-stream simulations and persisted-session recovery.
- No breaking API changes; new error/retry and memory-governance metadata remain additive.
