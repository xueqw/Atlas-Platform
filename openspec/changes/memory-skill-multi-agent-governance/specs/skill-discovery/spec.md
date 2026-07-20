## ADDED Requirements

### Requirement: Progressive Skill discovery
The system SHALL expose a Skill tree with category depth no greater than three and compact metadata before full Skill content. Full content SHALL be available only after a policy-approved selection.

#### Scenario: Candidate listing
- **WHEN** a planner requests candidate Skills
- **THEN** the response SHALL omit full instruction content and include use/do-not-use metadata

### Requirement: Explainable guarded routing
The system SHALL combine permitted manual bindings and automatic candidates, rank them with hybrid evidence and negative scenarios, and persist recall score, rerank score, reasons, decision, and selected IDs. It SHALL select none when confidence is low or the leading candidates are ambiguous.

#### Scenario: Ambiguous automatic candidates
- **WHEN** the leading permitted candidates are below the confidence threshold or have an ambiguous score margin
- **THEN** the router SHALL select no automatic Skill and SHALL persist the scores and rejection reason
