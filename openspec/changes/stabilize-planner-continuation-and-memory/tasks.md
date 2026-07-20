## 1. Model Stream Reliability

- [x] 1.1 Add typed empty-response detection, finish metadata, and non-SSE JSON fallback to the OpenAI-compatible adapter
- [x] 1.2 Add one bounded planner retry with run-event attribution and explicit terminal error behavior
- [x] 1.3 Prevent backend and frontend from persisting/rendering successful empty assistant turns

## 2. Confirmation and Proposal State

- [x] 2.1 Make advancing A2UI decisions idempotent and continuation-safe across duplicate frames
- [x] 2.2 Require pending confirmation or validated proposal evidence for proposal-related stages
- [x] 2.3 Add reconnect and confirmation-continuation regression tests

## 3. Planner Memory Integrity

- [x] 3.1 Add lazy goal-anchor derivation with source and transaction metadata
- [x] 3.2 Implement policy-governed memory candidate merge with additive de-duplication and conflict rejection
- [x] 3.3 Guard apply readiness behind validated proposal state and persist accepted/rejected update audit records
- [x] 3.4 Add explicit user-pivot, unrelated overwrite, legacy-load, and concurrent-session isolation tests

## 4. Integration and Delivery

- [x] 4.1 Add end-to-end planner tests for empty response retry success and exhausted failure
- [x] 4.2 Run backend, frontend reducer, build, OpenSpec strict validation, and live-browser recovery checks
- [x] 4.3 Update architecture/operations documentation with recovery and memory-integrity behavior
