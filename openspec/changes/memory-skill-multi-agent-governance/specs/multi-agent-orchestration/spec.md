## ADDED Requirements

### Requirement: Bounded ephemeral worker orchestration
The orchestrator SHALL be the only component allowed to create versioned WorkerSpecs. Workers SHALL use isolated context and Task/Result envelopes, defaulting to at most six workers, three concurrent workers, and two replans per run.

#### Scenario: Parallel independent tasks
- **WHEN** a DAG contains dependency-free tasks below the concurrency limit
- **THEN** the orchestrator SHALL dispatch them concurrently and preserve envelope/audit records

### Requirement: Intersected tool authority
The effective worker tool authority SHALL be the intersection of user grant, orchestrator grant, WorkerSpec, current authorization, and tool policy. Confirmation tickets SHALL be scoped, expiring, non-replayable, and invalidated by parameter changes.

#### Scenario: Changed high-risk parameters
- **WHEN** a worker changes the normalized parameters after confirmation
- **THEN** the prior authorization ticket SHALL be rejected and the operation SHALL require a new confirmation

### Requirement: Candidate assets are not automatically public
The system SHALL create Agent and Skill assets from successful trajectories only as candidates. It SHALL require redaction, evaluation, explicit approval, versioned publishing, rollback, and complete audit before availability.
