## ADDED Requirements

### Requirement: Governed memory ledger
The system SHALL append an idempotent, immutable ledger event for every governed memory mutation. Events SHALL record tenant identity, actor/run/worker context, event type, payload reference, timestamp, and idempotency key.

#### Scenario: Duplicate delivery
- **WHEN** a memory mutation is submitted twice with the same scope and idempotency key
- **THEN** the system SHALL return the original event and SHALL NOT create a second mutation

### Requirement: Bitemporal isolated semantic facts
The system SHALL retain valid-time and transaction-time ranges, evidence, confidence, sensitivity, consent, and supersession/tombstone information for semantic facts. Default reads SHALL require workspace, user, and agent scope and SHALL exclude tombstoned or inactive records.

#### Scenario: Historical correction
- **WHEN** a fact is corrected after its valid-time begins
- **THEN** the prior version SHALL remain auditable and the current query SHALL return only the active corrected version

### Requirement: Memory lifecycle safety
The system SHALL treat retrieved memory as untrusted context, support explainable retrieval, checkpoint session/working state durably, and require evaluation plus approval before any procedural candidate is published.

#### Scenario: Procedural candidate awaits governance
- **WHEN** repeated successful trajectories produce a procedural-memory or Skill candidate
- **THEN** the candidate SHALL remain unavailable to runtime selection until redaction, evaluation, and explicit approval succeed
