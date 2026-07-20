## Context

The planner has two execution families (legacy single-call/agentic and Plan+Loop) sharing one WebSocket transport, persisted planner sessions, and a frontend event reducer. In the observed failure, the OpenAI-compatible adapter received no visible content, emitted `done` anyway, and the legacy planner persisted an empty assistant turn. The frontend inferred a drafting/confirmation stage from message count while no proposal existed. Separately, `_merge_memory_update` trusted every non-empty model-authored string and replaced the original stock-selection goal with an unrelated customer-service summary.

The implementation must retain the current AgentScope/OpenAI-compatible integrations, SQLite/PostgreSQL compatibility, Redis working-state fallback, existing tenant/conversation boundaries, and the additive event contract.

## Goals / Non-Goals

**Goals:**

- Make every planner turn terminate as success-with-content/proposal or explicit failure; never as a successful empty turn.
- Retry a transient empty model response once without duplicating user messages, confirmations, artifacts, or run events.
- Make A2UI confirmation continuation deterministic across live sockets, reconnects, and duplicate acknowledgements.
- Keep the user's original task goal as an evidence-backed anchor while allowing additive refinements.
- Record accepted and rejected memory updates with source, turn, reason, and transaction time.
- Preserve tenant/conversation isolation and existing storage/runtime compatibility.

**Non-Goals:**

- Replacing AgentScope or the existing providers.
- Redesigning the entire planner UX.
- Adding a new external memory product or vector database.
- Automatically repairing historical corrupted sessions without explicit recovery.

## Decisions

### 1. Model streams use an explicit completion contract

The OpenAI-compatible adapter will count visible content, retain the provider finish reason, support both SSE and a valid JSON completion fallback, and raise a typed `EmptyModelResponse` when HTTP success produces no usable response. It MUST NOT emit `done` after an empty completion.

The planner orchestration layer will retry that typed transient failure once. The retry is part of the same logical turn and run ID. If it still fails, the backend emits a failed run event and a WebSocket `error`; it does not persist an assistant message or advance stage.

Alternative considered: retry inside the HTTP adapter. Rejected because orchestration owns run events, user-visible attribution, idempotency, and provider fallback policy.

### 2. Persist only meaningful assistant turns

The legacy and Plan+Loop paths will share a small invariant: an assistant message requires non-whitespace content, a structured proposal, or a structured control result. Empty text is not a message. The frontend reducer will also ignore a defensive empty `done` and surface a recoverable error if it occurs from an older backend.

### 3. Proposal state is artifact-backed

`awaiting_confirmation` and `ready_to_apply` require either a pending A2UI decision with a non-empty preceding assistant response or a validated proposal. Message count alone is not authoritative. Session persistence and reconnect derive stage from proposal/control state, not the number of assistant turns.

### 4. Confirmation continuation is a structured state transition

The A2UI response remains idempotent and persists the human decision first. An advancing choice carries a stable continuation token/request ID. The immediately following continuation turn must observe the recorded decision and may be retried without duplicating it. Duplicate A2UI responses acknowledge the existing decision rather than appending another ledger record.

The backend persists `pending_continuation` (`request_id`, `choice`, `token`,
canonical content, stable `run_id`, lease metadata, and
`pending|processing|completed` status) in `planning_state_json` before
acknowledging the decision. Session REST and WebSocket restore expose resumable
pending/processing records. The message entry claims under the shared
working-state lock, but marks the command completed only in the same durable
write as an assistant/control/proposal result or explicit terminal failure. A
processing record without a live local owner can be replayed with the same
token/run ID; token-tagged user and assistant transcript rows make that replay
idempotent at the committed-turn boundary.

### 5. Planner memory uses anchor + governed deltas

The first substantive user request creates `goal_anchor` with source message reference and transaction time. Model memory updates are candidate deltas:

- Additive fields (constraints, preferences, risks, decisions) merge with normalized de-duplication.
- `requirement_summary` may refine the anchor only when it retains anchor concepts or is directly supported by recent user text.
- A conflicting replacement is rejected, recorded in `memory_update_audit`, and cannot change the persisted session title/summary.
- Proposal/readiness state cannot be marked ready without a validated proposal.

This is the planner-local application of the existing ledger/views/policy principle: the transcript and decision ledger are the ledger, the planner memory snapshot is a materialized view, and the merge policy governs writes.

Alternative considered: make `requirement_summary` immutable. Rejected because legitimate user refinements must remain possible.

### 6. Compatibility and rollout

New memory metadata is stored in `planning_state_json` so no destructive migration is required. Old sessions without anchors derive one from `user_request` or the first substantive user message on load. Feature behavior is safe by default and uses existing Redis/PostgreSQL/SQLite persistence paths.

## Risks / Trade-offs

- [Over-strict semantic guard rejects a valid pivot] → Explicit recent user statements can replace the anchor; rejected candidates remain auditable.
- [Retry duplicates provider cost] → Exactly one retry, only for zero-token/malformed successful responses.
- [Older frontend sends duplicate confirmation/continue frames] → Stable request IDs and idempotent decision merge.
- [Legacy sessions already contain polluted summaries] → Derive anchors from original user messages; do not silently rewrite historical content.
- [Provider returns non-standard JSON] → Accept only documented OpenAI-compatible completion shapes; otherwise fail visibly.

## Migration Plan

1. Deploy additive backend contracts and tests.
2. Deploy defensive frontend reducer and progress-state changes.
3. On session load, lazily derive missing anchors without changing original transcript.
4. Observe empty-response retry/failure run events and rejected-memory audit counts.
5. Roll back by reverting the additive code; persisted metadata remains ignorable.

## Open Questions

None blocking. Cross-provider automatic fallback remains a future policy decision; this change retries the selected provider once and fails explicitly.
