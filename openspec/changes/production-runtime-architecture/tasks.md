## 1. Specification and Infrastructure

- [x] 1.1 Create and validate OpenSpec proposal, design and capability specs
- [x] 1.2 Add PostgreSQL/pgvector, Redis and MinIO production services and health checks
- [x] 1.3 Add secret-file and production storage configuration

## 2. Persistence and Memory

- [x] 2.1 Persist knowledge originals and Agent archives through workspace-scoped object storage
- [x] 2.2 Replace process-local temporary state with a TTL state store
- [x] 2.3 Add short-term and long-term Agent memory APIs with tenant and user isolation

## 3. Runtime Security

- [x] 3.1 Execute code Agents in constrained one-shot Docker containers
- [x] 3.2 Harden HTML preview sandbox and CSP
- [x] 3.3 Add dependency health and runtime security tests

## 4. Operations

- [x] 4.1 Add backup, verification and restore scripts
- [x] 4.2 Run backend, frontend and Compose validation
- [x] 4.3 Prepare the migration, backup, restore, offline-image and deployment runbook
- [ ] 4.4 Back up, migrate and deploy the verified build when the company server is reachable
- [ ] 4.5 Run post-deployment health and functional smoke tests
