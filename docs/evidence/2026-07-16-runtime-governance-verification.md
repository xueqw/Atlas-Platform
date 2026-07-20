# Runtime / Governance verification evidence — 2026-07-16

## Automated suites

- Main API: `272 passed, 5 skipped in 15.46s`. The skipped tests are opt-in real-service probes; the PostgreSQL/pgvector/Redis and Docker probes were then executed against the isolated acceptance environment below.
- Workbench Web: `3` Vitest files / `10` tests passed; TypeScript and production Vite build passed. The visible Runtime mission-control coverage distinguishes the ReAct loop from Multi-Agent Orchestrator, Subagents, independent Reviewer, and verdict rendering.
- AgentGateway backend: `603 passed` with no failure and one Starlette deprecation warning.
- AgentGateway frontend: all `16` Node test files passed; TypeScript and production Next.js build passed, and all ten routes were generated.
- AgentGateway Runtime Bridge contract: `1 passed`; embedded workbench test chat, DAG test chat, and node debug dispatch Atlas Runtime commands while standalone operation retains the legacy transport.

## Real Redis loss probe

An isolated Redis server was started on `127.0.0.1:6387`, persistence disabled, using database 15. The release-only test executed a durable Runtime run, stored its hot cursor in Redis, flushed Redis completely, constructed a new Runtime service, and restored terminal state plus ordered event replay from the SQL authority. The governed short-memory API additionally has deterministic coverage for PostgreSQL-authoritative checkpoints, optimistic versions, hot-cache loss/repopulation, and deletion.

Command:

```bash
ATLAS_TEST_REDIS_URL='redis://127.0.0.1:6387/15' \
python -m pytest -q tests/test_runtime_redis_integration.py
```

Result: `1 passed`.

## Managed acceptance environment

The missing runtime dependencies were installed and recorded in `Brewfile.acceptance`; `brew bundle check` reports that the file is satisfied. Tested host tools were Colima 0.10.3, Docker CLI 29.6.1 / Engine 29.5.2, Compose 5.3.1, PostgreSQL client 16.14, pgvector formula 0.8.5, Redis 8.8.0, MinIO, `mc`, aria2 1.37.0_2 and ShellCheck 0.11.0. Application dependencies remain pinned by `api/requirements.txt` and the JavaScript lockfiles.

An isolated Colima profile named `atlas-acceptance` was created with 4 CPU and 6 GiB RAM. The acceptance Compose projects bind only to loopback on PostgreSQL `15432`, Redis `16379`, and MinIO `19000/19001`. Stateful commands reject project names outside `atlas-acceptance[-*]`. The source and restore projects use independently generated secret files; all three source/target secret hashes were verified different without disclosing their values.

The target images and immutable digests were:

- PostgreSQL 16 + pgvector: `sha256:1d533553fefe4f12e5d80c7b80622ba0c382abb5758856f52983d8789179f0fb`
- Redis 7 Alpine: `sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99`
- MinIO: `sha256:a1ea29fa28355559ef137d71fc570e508a214ec84ff8083e39bc5428980b015e`
- MinIO client: `sha256:aead63c77f9db9107f1696fb08ecb0faeda23729cde94b0f663edf4fe09728e3`
- Atlas Python runner: `sha256:dc17178f5bb0ecdbee66d654fad260760deef16e1d1adea145867467ce6bd364`

Docker Hub was unreachable from the acceptance network, so the same named upstream images were pulled through an explicitly configured DaoCloud mirror. The mirror selection is never silent and is documented in the runbook.

## Real PostgreSQL/pgvector and Redis data-plane gates

The real-service suite exercised PostgreSQL 16.14 and pgvector 0.8.5 with `Vector(1024)` Skill and Semantic Memory rows, database-side threshold/order/limit, workspace isolation, valid/transaction-time address correction, a newly opened LangGraph saver, eight concurrent idempotent Runtime starts, ordered event cursors, Redis TTL, complete Redis DB loss, and PostgreSQL-authoritative Session/Runtime/Event reconstruction.

Result:

```text
.... [100%]
4 passed in 0.97s
```

The dedicated ADR compatibility Spike was also run against the restored PostgreSQL target:

```text
. [100%]
1 passed in 0.27s
```

## Adaptive execution and independent review extension

The adaptive Runtime extension was specified in OpenSpec change
`adaptive-agent-execution-strategies` and inspected through CodeGraph before
implementation. The server-owned router now selects exactly one immutable
strategy per run: bounded ReAct, sequential Plan-Execute-Review, or
Multi-Agent Plan-Execute-Review. Explicit strategy requests remain subject to
mandatory authorization and risk floors.

Complex execution uses strict Plan, Worker, Task, ReviewRequest, ReviewResult,
and RevisionRequest schemas. The Orchestrator creates isolated child
RuntimeRuns for workers, schedules independent DAG branches concurrently, and
creates a separate ephemeral Reviewer RuntimeRun with an empty tool registry.
Review output is fail-closed unless it matches the parent identity, comes from
the expected independent reviewer, covers every planned task and acceptance
criterion, and contains one of `PASS`, `REVISE`, `REPLAN`, `REJECT`, or
`ESCALATE`. Revision/replan budgets are bounded, and escalation can resume only
after an explicit actor-bound approve/deny decision.

Automated coverage includes deterministic and model-assisted routing safety,
explicit overrides, malformed/cyclic DAGs, all five review verdicts,
independence and complete-coverage checks, concurrent durable child runs,
cross-tenant artifact rejection, immutable strategy persistence,
resume/idempotency, and prevention of legacy fallback after a persisted side
effect. A real LangGraph `InMemorySaver` test exposed and closed strict JSON
checkpoint restoration for enum/tuple state.

The first adaptive-only independent review then found three blocking gaps and
two schema/event gaps. All five were closed before final acceptance:

- a Reviewer `PASS` can no longer override a failed deterministic finding;
- worker outputs now cross the parent boundary as strict, scope-bound
  `WorkerResultEnvelope` objects, and planned Worker input schemas are
  preserved inside the durable TaskEnvelope wrapper; nested input/output
  schemas are enforced by the pinned JSON Schema 2020-12 validator, while
  malformed schemas fail closed;
- every execution Worker emits `worker.created`, while `plan.created` retains
  the complete immutable plan version for event-ledger reconstruction;
- a declared child write first persists `side_effect.boundary_started` in the
  parent Runtime, preventing legacy replay even if the child crashes during the
  call;
- an unapproved write pauses, creates exact-call child tickets, and is surfaced
  through the existing actor-bound Runtime interrupt. The integration test
  proves approve resumes the same physical task and executes the write exactly
  once, while deny cancels without executing the write. A simulated crash
  between graph-pause persistence and parent-interrupt attachment proves resume
  idempotently recovers the same deterministic-nonce interrupt and still
  rejects an unbound decision. A second fault injection commits the approved
  interrupt first and omits the parent-state update; retrying the same complete
  binding recovers only while the parent still advertises that paused
  interrupt, while a post-progress replay remains rejected.

The production PostgreSQL saver was then exercised against the isolated
PostgreSQL 16 acceptance target. Both the original ReAct checkpoint/restart
probe and the new complete Plan-Execute-Review checkpoint/restart probe passed;
the restarted process did not duplicate planner, executor, reviewer, model, or
tool nodes:

```text
.. [100%]
2 passed in 1.01s
```

Strict OpenSpec validation passed for
`adaptive-agent-execution-strategies`, `langgraph-runtime-foundation`, and
`memory-skill-multi-agent-governance`. `LANGGRAPH_RUNTIME_ENABLED` and
`ADAPTIVE_RUNTIME_ENABLED` both remain `false` by default, so rollout remains
an explicit operational decision with the legacy path available for rollback.

The decoupled adaptive-runtime QA reviewer initially failed the delivery gate
and drove five repair rounds covering deterministic review precedence, exact
write confirmation, pre-write side-effect persistence, strict Worker schemas,
event reconstruction, nested JSON Schema enforcement, and both parent
interrupt crash windows. Its final independent verdict is **PASS, P0 = 0,
P1 = 0, P2 = 0**. It independently ran the Adaptive/Multi-Agent targeted
matrix (`23 passed`), `pip check`, and the double-crash fault injection on a
real LangGraph template with `InMemorySaver`.

## Real MinIO, production health and Docker sandbox gates

The API was started with `ENVIRONMENT=production`, real PostgreSQL/Redis/MinIO, file-backed secrets, and the required Docker runner. `/api/health` returned:

```json
{"dependencies":{"code_runner":true,"database":true,"object_storage":true,"redis":true},"service":"atlas-api","status":"ok","version":"0.4.0"}
```

Production startup now fails closed when object storage is required but the backend is `local` or misspelled. A negative process probe confirmed `ENVIRONMENT=production + OBJECT_STORAGE_REQUIRED=true + OBJECT_STORAGE_BACKEND=local` is rejected with `Production object storage requires OBJECT_STORAGE_BACKEND=minio`; an unknown backend also returns an unhealthy ping instead of silently becoming local storage.

A real MinIO probe wrote two workspace-scoped object keys, listed only workspace A's prefix, confirmed workspace B was absent, read workspace A back with an identical SHA-256, and removed both probe objects. Result: `cross_tenant_visible=false`, `sha256_match=true`.

The runner image was built from the pinned Python 3.11.13 Alpine base. The live Docker probe inspected and behaviorally verified disabled networking, read-only root/workspace, writable bounded `/tmp`, all capabilities dropped, `no-new-privileges`, non-root UID, memory/swap/CPU/PID/init limits, no host secret inheritance, and forced cleanup after an infinite-loop timeout. Result: `1 passed in 6.00s`, with no `com.atlas.runner=true` container leak.

## Backup and isolated restore rehearsal

The acceptance flow performed source seed, PostgreSQL custom-format dump, MinIO object mirror with per-object SHA-256 index, explicit Redis exclusion, source shutdown, fresh target creation, transactional PostgreSQL restore, MinIO restore/readback comparison and Redis reset. The final evidence is under the git-ignored acceptance root:

- `.acceptance/atlas-acceptance-ops-20260716/backups/20260716T110730Z-bc1cecd92c31/manifest.json`
- `.acceptance/atlas-acceptance-ops-restore-20260716/evidence/restore-20260716T110730Z-bc1cecd92c31.json`

The manifest records Git `bc1cecd92c31f105ec3b966930eb5b92e893c53d`, PostgreSQL 16.14, pgvector 0.8.5, a verified dump hash, one 29-byte MinIO sentinel and `redis.included=false`. Restore result was `pass`: PostgreSQL sentinel restored, MinIO content index matched, Redis remained empty and was not restored, and measured RTO was 2 seconds. This is below the documented 4-hour target; the RPO remains the last successful scheduled backup (24-hour target for daily operation).

## Production backup/restore entrypoint closure

The legacy production entrypoints were replaced so the deployed runbook no longer archives live MinIO private volumes or Redis data. The production backup now uses custom `pg_dump`, object-level `mc mirror`, a per-object SHA-256 index, an atomic temporary-directory rename, a schema-versioned manifest, exact checksums and a matching `COMPLETE` marker. Redis is explicitly excluded; Agent code is path-validated and archived; only non-sensitive example/Compose configuration is included.

The rewritten production scripts were then exercised against acceptance-only Compose projects, not production data:

- source project: `atlas-acceptance-ops-restore-20260716`;
- production-script backup: `.acceptance/production-script-backups/20260716T120031Z-bc1cecd92c31`;
- isolated restore project: `atlas-acceptance-prodrestore-20260716`;
- manifest: PostgreSQL 16.14, pgvector 0.8.5, 144,458-byte logical dump, MinIO S3-object transport, one 29-byte object, `redis.included=false`, Agent code included, sensitive config excluded;
- backup and restore both required an explicit `ATLAS_COMPOSE_PROJECT`; the manifest recorded source project `atlas-acceptance-ops-restore-20260716`, and every Compose service label was checked against the selected project;
- verify-only mode passed, then an apply attempt with a mismatched target was rejected before writes while its Redis sentinel remained present;
- the successful write restore required `ATLAS_RESTORE_TARGET=atlas-acceptance-prodrestore-20260716`, exactly matching `ATLAS_COMPOSE_PROJECT`, plus a confirmation bound to that project and the exact backup ID;
- restored PostgreSQL returned server `160014`, pgvector `0.8.5` and `postgres-durable` sentinel;
- MinIO restore/download index matched inside the script;
- Redis was seeded with stale state before restore, then `FLUSHALL SYNC`, `DBSIZE=0` and empty keyspace were verified;
- restored Agent code matched the source tree byte-for-byte; all three target containers remained healthy.

`bash -n`, `shellcheck -x`, and `git diff --check` passed for the production and acceptance scripts. The production runbook now directs drills to the isolated acceptance flow and reserves `scripts/restore.sh --apply` for an explicitly named maintenance target.

## Code-level hardening closed after the first independent review

- Application-development host/iframe Runtime Bridge now executes and streams `start`, `resume`, `cancel`, and `subscribe` with origin, source, schema, request, cursor, explicit Agent mapping, and interrupt anti-replay validation. Embedded AgentGateway workbench, DAG test chat, and node debug use this bridge; standalone mode retains the original WS/REST transport.
- Runtime Skill routing performs tree-first lexical + persisted pgvector retrieval and reranking; create/update paths apply full metadata validation.
- Session Context uses a PostgreSQL ledger/checkpoint authority and a Redis TTL hot copy; Redis failure cannot roll back the durable write.
- Repeated successful redacted trajectories create non-public Skill candidates; evidence is scope-bound and server replay ignores client pass-rate claims. The default offline runner executes persisted replay fixtures and verifies output schema, expected output, and assertions; missing fixtures fail closed.
- Server-authored automatic experience fixtures contain only redacted structural fields; three successful trajectories can complete candidate, redaction, replay, human approval, versioned Skill publication, and rollback governance. A corrupt fixture fails closed.
- Semantic-memory retrieval uses pgvector database-side distance, scope/time/policy filters, threshold, order, and limit on PostgreSQL; Python cosine is restricted to SQLite compatibility tests.
- Worker execution uses the unified Runtime with pinned AgentVersion, isolated schemas, trusted static/dynamic MCP registries, read-auto/write-confirm policy, permission intersection, non-replayable tickets, shared foreground/background bounded replan, dependency-warning audit, and persistent cross-process cancellation. Persistent cancellation preempts the local Runtime/model await; an in-flight write boundary is audited as `unknown_not_rolled_back` and no later step executes.
- Runtime resume payloads require the complete interrupt binding tuple at both HTTP and iframe-bridge boundaries; partial tuples fail with a normalized validation error before service execution.

The earlier decoupled quality review found no remaining code-level P0 or P1 and passed the code delivery gate. A second independent evidence review reproduced the real data-plane, checkpoint, Docker, MinIO, health, API, AgentGateway, frontend and OpenSpec gates, then correctly blocked release on the legacy production backup/restore path and local-object-storage deployment drift. Both findings were resolved and independently rechecked. Final result: **PASS, P0 = 0, P1 = 0**. The Runtime feature flag remains disabled by default, preserving the explicit rollout/rollback decision rather than enabling user traffic as a side effect of acceptance.
