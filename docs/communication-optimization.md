# Worker/API communication optimization

## IMPLEMENTATION STATUS

PASS for implementation and local regression verification. Production deployment is NOT verified. No real Mac worker, benchmark, Render deployment or cloud bucket was run. Existing server process, network settings and production database schema were not intentionally restarted/changed. Apply the additive migration before deploying this revision.

## TARGET

4 Workers × 5 hours/day × 30 days = 600 worker-hours/month. macOS local Docker evaluation; existing Worker endpoints and JSON payloads retained. Public HTTPS API origin is supplied by existing SERVER_URL, not hard-coded. Local HTTP development remains supported. Legacy SSH Git metadata is converted to HTTPS on the worker before clone, preserving commit verification and the server's stored URL. Public repositories need no SSH credentials; private repositories still require separately authorized HTTPS repository access.

## FILES CHANGED

| File | Change |
| --- | --- |
| workers/evaluator/config.py | Independent idle heartbeat and bounded polling settings; old polling minimum and active heartbeat retained |
| workers/evaluator/scheduling.py | Monotonic independent deadlines, exponential bounded idle backoff, jitter, reset on job |
| workers/evaluator/main.py | Scheduler integration; heartbeat no longer sent for every claim; persistent identity unchanged |
| workers/evaluator/lease.py | Transient network/5xx retries only inside last acknowledged safety window; 4xx immediately fences |
| workers/evaluator/inputs.py | Approved legacy Git SSH URL converted to HTTPS on the worker |
| apps/api/agentbenchx/config.py | Reaper/offline/pool/storage settings, secret masking |
| apps/api/agentbenchx/db.py | Explicit bounded pool, pre-ping retained |
| apps/api/agentbenchx/main.py | Auth takes Worker row lock once; configurable background reaper; storage proxy through existing authenticated routes |
| apps/api/agentbenchx/services.py | Reuse auth lock, no reaping inside claim, transition events, storage abstraction, post-storage expiry check |
| apps/api/agentbenchx/models.py | Nullable internal last_event on RunnerJob |
| migrations/versions/003_job_event.py | Add/drop last_event, preserving existing rows |
| apps/api/agentbenchx/storage.py | Local and private S3-compatible implementations, logical keys, conditional immutable writes |
| .env.example; workers/evaluator/.env.example | Configuration documentation only; actual environment files preserved |
| pyproject.toml; requirements.lock | boto3 and pinned transitive dependencies |
| tests/test_api.py | Explicit reaper invocation for expiry recovery test |
| tests/test_worker.py | Transient lease failure/rejection and HTTPS source regression |
| tests/test_optimization.py | Concurrent four-job claims, crash recovery, SQL execution counters, timing and load budgets, delayed storage expiry |
| tests/test_storage.py | Local immutability, real boto3 request serialization with Stubber, sanitized failures, authenticated proxy/restart fixture |
| scripts/simulate_worker_load.py | Repeatable virtual-time before/after simulation using production IdleSchedule |
| docs/load-simulation.json | Simulation output, explicitly NOT live cloud/Mac measurement |
| docs/communication-optimization.md; README.md | Evidence, configuration, deployment limitations |

## WORKER POLLING

Before: heartbeat → claim → sleep 5 seconds if empty. Latencies extend this period. There is no 20-second independent idle heartbeat timer in the old implementation.

After: empty delays grow 5, 8–10, 16–20, 24–30 seconds (bounded jitter), then remain 24–30. Successful claim immediately resets the backoff; no unconditional sleep after a job. Initial poll/heartbeat gets 0–5 seconds of startup jitter. Idle heartbeat has its own deadline and wakes the scheduler even when the next poll is later. First enrollment remains one request per identity. Normal maximum discovery delay at the cap is 30 seconds plus network/processing delay; this is not a hard SLA during outages.

WORKER_POLL_INTERVAL remains the default minimum when WORKER_IDLE_POLL_MIN_SECONDS is omitted. Omitted maximum resolves to max(30, minimum), preserving legacy configurations whose minimum exceeds 30. Explicit maximum smaller than minimum is rejected.

## WORKER HEARTBEAT

Before: every idle loop (nominal 5 seconds), one PUBLISH every time.

After: WORKER_IDLE_HEARTBEAT_SECONDS=45 with 0.9–1.0 jitter (40.5–45 seconds). Worker offline detection is independent: WORKER_OFFLINE_SECONDS=120 minimum. This allows more than two nominal idle intervals before being considered offline. Default reaper detection follows within up to another 30 seconds plus service delay. Active job heartbeats/results update liveness, so idle heartbeat is paused during evaluation.

Worker status events only publish on a real status transition. Dashboard's existing 15-second REST refresh still updates heartbeat timestamps; event-triggered refresh still shows state changes promptly. Registration and graceful daily ONLINE/OFFLINE transitions remain visible.

## ACTIVE LEASE HEARTBEAT

Before/after: lease remains 120 seconds; periodic delay min(20, lease/4)=20 seconds. Entry and four normal phase changes also renew. HTTP time and thread scheduling affect actual periodic counts. Last acknowledged send time + 0.75×lease is the conservative safety deadline (90 seconds by default). A watchdog cancels blocked execution at that deadline. Failure does NOT extend it. Transient network/5xx errors retry with up to two-second waits while there is time; definitive 4xx rejects immediately. An acknowledgment arriving after the safety deadline cannot revive a cancelled lease.

A one-minute outage can recover if it ends inside the remaining safety window. A two-minute outage cannot safely preserve the same 120-second lease: cancel/fence and server requeue are the correct outcome. Result replay/idempotency and the maximum attempt limit remain in place. Reaper terminal state at exhausted attempts is FAILED, with an expiry error message. Earlier attempts are TIMEOUT and get a new queued job.

## REDIS

RedisQueue still implements ZADD, ZREM and PUBLISH; PostgreSQL remains authoritative. No added heartbeat SET/EXPIRE/cache command. Claim: ZREM + worker BUSY event. Normal full evaluation phase sequence: five PUBLISH commands, irrespective of repeated lease renewals. Result: two PUBLISH commands. New job submission: ZADD + PUBLISH. Phase memory is stored in the same RunnerJob update, not a Redis key or process-local cache; persists across API restart. Redis errors remain nonfatal, and REST refresh reconciles missed events.

Normal complete job lifecycle: 2 enqueue + 2 claim + 5 phase + 2 result = 11 application Redis commands. Early failures can have fewer phase events; retries add executions/enqueues. There is no claimed bound for arbitrary failure/restart/event patterns or Redis transport retransmissions.

## DATABASE

Per-request counts below exclude pool pre-ping and transaction-control protocol messages. After counts were checked with SQLAlchemy before_cursor_execute and commit events on real PostgreSQL in disposable schemas; INSERT counts use a one-test result fixture.

| Request | Before SQL | After SQL | After transaction |
| --- | --- | --- | --- |
| Normal idle heartbeat | SELECT 3 + UPDATE 1 | SELECT 2 + UPDATE 1 | 1 commit |
| Empty claim | SELECT 6 | SELECT 3 | 1 commit instead of 3 |
| Successful claim | SELECT 9 + UPDATE 2 | SELECT 6 + UPDATE 2 | 1 commit instead of 3 |
| Job heartbeat | SELECT 3 + UPDATE 2 | SELECT 2 + UPDATE 2 | 1 commit |
| Agent GET | SELECT 5 | SELECT 4 | 1 read transaction/rollback |
| First result, one test | SELECT 6 + INSERT 5 + UPDATE 2 | SELECT 5 + INSERT 5 + UPDATE 2 | 1 commit |
| Duplicate accepted result | SELECT 4 | SELECT 3 | 1 read transaction/rollback |
| Unchanged reaper cycle | SELECT 2 | SELECT 2 | 2 commits retained |

Worker auth now acquires the existing Worker FOR UPDATE lock before checking token hash. Services reuse that lock; job FOR UPDATE / queued SKIP LOCKED are preserved. All Worker operations have the same lock order (Worker then job). Reaper intentionally commits job changes before acquiring Worker locks: merging these two transactions would introduce an opposing lock order and deadlock risk. Identity-map/expire_on_commit=False behavior is unchanged. No ORM relationship lazy loads are introduced.

DB_POOL_SIZE=4, DB_MAX_OVERFLOW=1, DB_POOL_TIMEOUT=5 seconds, pool_pre_ping=True: at most five connections per API process, not an assurance about the customer's actual PostgreSQL quota. Multiple API processes multiply that allocation. Consult actual DB max_connections and other consumers before deployment. New connections, pre-ping SELECT 1, BEGIN/COMMIT/ROLLBACK and driver batching mean wire round trips remain UNKNOWN from this model. A healthy reused connection adds one pre-ping per transaction; new connection initialization differs.

## REAPER

Claim does not call reap(). REAPER_INTERVAL_SECONDS=30 replaces a hard-coded 20-second background delay. Defaults keep expired jobs recoverable after lease expiration plus at most one interval/processing delay. Dedicated reaper is per API process; do not accidentally count it once per Worker.

For one API process running 24h × 30d: before nominal 129,600 independent reaper cycles / 259,200 SELECT / 259,200 commits; after 86,400 / 172,800 / 172,800. Claim-induced reaper cycles (432,000 for old idle scenario) are eliminated. No-change cycles publish zero Redis commands. Changed rows/retries/offline transitions add writes and events. A sleeping Render service does not run this timer; the values above deliberately budget an always-running server.

## LOAD ESTIMATE

The exact actual HTTP/SQL monthly totals cannot be inferred from only jobs/hour. Required unknowns include job duration, input/build/upload time, RTT, failures and number of tests. Reproducible fixture assumptions: four workers, 30 separate five-hour sessions each, graceful daily stop, four first enrollments, retained identity thereafter; jobs arrive at 3600/rate seconds, each lasts 60 seconds with one test and seven lease calls (five phase calls plus periodic t=20 and t=40); no latency/retries; deterministic random seeds. Startup/shutdown and jitter are included. This is a virtual-time model, not four real Macs or a throughput stress test. Runtime settings are exercised through the actual IdleSchedule. Per-call SQL weights are verified separately against the actual API/PostgreSQL.

See load-simulation.json for individual HTTP/SQL/transactions/commits/rollbacks/claims/lease calls/results/writes, one-worker idle, and before/after scenarios. Transactions include read rollbacks. Actual INSERT batching with many tests is UNKNOWN; do not substitute INSERT row counts for statements.

| Jobs/month | Worker HTTP | Redis incl. job enqueue | Worker SQL | Worker + enqueue + 24h server reaper SQL | Worker transactions | Results | Artifact writes |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 130,959 | 240 | 392,869 | 565,669 | 130,959 | 0 | 0 |
| 600 | 135,415 | 6,840 | 419,437 | 594,637 | 135,415 | 600 | 3,600 |
| 3,000 | 153,952 | 33,240 | 527,848 | 712,648 | 153,952 | 3,000 | 18,000 |
| 6,000 | 177,353 | 66,240 | 664,051 | 860,851 | 177,353 | 6,000 | 36,000 |

Worker HTTP excludes admin submission HTTP (one per job in this fixture), health probes, browser calls, Git/Docker downloads and object storage SDK HTTP. SQL includes SELECT/INSERT/UPDATE; control statements and pre-ping remain separate. Each successful artifact write is one local file write OR one object PUT, depending on selected backend. S3 mode also needs one agent GET/job, agent upload PUTs, and artifact GETs on user access. Six artifacts are still transmitted in one Worker JSON result, never six Worker uploads.

## Before / After (idle)

| Metric/month | Before | After | Reduction |
| --- | ---: | ---: | ---: |
| Worker HTTP | 864,124 | 130,959 | 84.84% |
| Worker Redis | 432,124 | 240 | 99.94% |
| Worker SQL | 4,320,484 | 392,869 | 90.91% |
| Worker transactions | 1,728,124 | 130,959 | 92.42% |
| Independent reaper SELECT (24h server) | 259,200 | 172,800 | 33.33% |

For arbitrary measured intervals: let H=idle/status heartbeat calls, E=empty claims, J=completed attempts, R=all lease calls, I=actual result INSERT statements, G=initial registrations. Excluding failures and interval-crossing incomplete jobs, Worker HTTP=H+E+3J+R+G and Worker SQL=3H+3E+19J+4R+I+G. Normal five-phase Redis with daily lifecycle and job generation is 11J+240. Periodic renewals do not generate extra PUBLISH, so Redis normal-path budgeting does not depend on job duration.

One-worker idle virtual-time result: HTTP 32,748, SQL 98,242, Redis 60 /30d. Four workers differ slightly from exactly four times these HTTP/SQL values because each worker/day uses independent jitter.

### Other server activity

Health endpoint adds one explicit DB SELECT and Redis PING per probe; probe count UNKNOWN. Upstash excludes PING from billed commands. Authentication/connection handshakes and reconnects are separate. WebSocket SUBSCRIBE occurs per connection, not per get_message wait; JSON ping frames are not new Redis PING or HTTP requests. Dashboard refresh remains every 15 seconds and on state/phase events. For a single tab open five hours/day for 30 days, periodic refresh alone is nominally 36,000 requests plus initial loads and state events. Dashboard query cost depends on stored jobs/evaluations and is excluded from Worker totals. No fixed number of open tabs or health interval was invented.

## FREE LIMIT CHECK

Upstash: PASS for modeled application command budget, not a live account usage assertion. Official Free limit checked 2026-09-14: 500,000 monthly commands. Idle=240; 600 jobs=6,840; 3,000=33,240; 6,000=66,240 including enqueue. High case is 13.248% of Free limit, leaving 433,760 commands; all modeled cases are below both 250,000 and the preferred 150,000 target. Bytes, retries, abuse and additional subscribers must be observed in production.

Render: FAIL for unconditionally claiming production readiness; actual deployment/tier/runtime/traffic unverified. A Render Web Service supports the architecture. Render Free can sleep after 15 minutes of inactivity, takes about a minute to wake, and has ephemeral files; its workspace has 750 free instance hours/month. Worker five-hour operation is not a guarantee the API runs only five hours (dashboard/other traffic can keep it awake). Default HTTP timeout can expire during cold start; existing first-enrollment failure is not automatically restarted by this change. A supervised worker restart or always-on service must be validated before production.

PostgreSQL: PASS for local integration correctness/concurrency; FAIL for a claimed live cloud capacity/free-tier guarantee because actual DB provider, available connections, storage and latency are unverified. Five connections/API process is an explicit bounded allocation, not a guarantee of the production quota. If selecting Render Free Postgres specifically, it expires 30 days after creation and has a 1GB limit.

Storage: PASS for local backend and mocked S3/API contract tests; FAIL for claimed live durable storage readiness until a private bucket, server credentials, object access and retention are actually verified. No bucket was created or made public.

Sources:
- https://upstash.com/pricing/redis
- https://render.com/docs/free
- https://render.com/docs/postgresql-creating-connecting
- https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/put_object.html
- https://developers.cloudflare.com/r2/examples/aws/boto3/

## STORAGE AND DEPLOYMENT

1. Deployers must apply `alembic upgrade head` (003_job_event) before starting this API revision. Existing running production process/schema were left intact. Old Worker endpoints/payloads remain compatible; old Workers still poll more frequently until upgraded. To revert, restore previous API first, then optionally downgrade the additive column; identity and existing storage keys need no edits.
2. Keep LOCAL for development. For Render production configure STORAGE_BACKEND=s3, S3_BUCKET, HTTPS S3_ENDPOINT_URL (omit for AWS), S3_REGION, and server-only S3_ACCESS_KEY_ID/S3_SECRET_ACCESS_KEY using the deployment secret store. No values were written to actual .env files. boto3 is included in the project and deployment requirements lock.
3. Provision a PRIVATE bucket: disable R2 public/custom-domain access, or enable S3 Block Public Access. Restrict the server principal to GetObject/PutObject on `agents/*` and `evaluations/*`. No public ACL, worker cloud credential, bucket URL, presigned credential or direct public download is returned. API uses an authenticated server proxy. Actual bucket policy/public access is external state that these SDK tests cannot certify.
4. Existing DB storage paths are logical keys. If switching an existing local installation to S3, copy existing agent/artifact bytes to the identical keys before selecting s3. Automatic migration of existing files was not performed. Local Git source mirrors remain rebuildable local caches; historical commits no longer reachable upstream still depend on preserved source data. This change does not pretend that all local Git caches are persistent object storage.
5. S3 writes use IfNoneMatch='*' to preserve immutable objects. Unsupported conditional-write providers fail safely. Partial artifact writes/DB commit failures can leave unreferenced objects; there is no distributed storage/DB transaction or automatic orphan garbage collector. Result idempotency prevents duplicate accepted evaluations. After potentially slow storage work, lease expiration is checked again before committing a result. Storage credentials never reach Workers.
6. Reuse the existing public HTTPS SERVER_URL origin (no /api/v1 suffix) when provisioning Macs. No IP, VPN, SSH administrative connection, port mapping, manual Worker ID or bootstrap token was introduced. Registration is still the preexisting open enrollment endpoint; this optimization does not introduce public admission controls or enforce a hard maximum of four registered identities. Public admission risk remains a separate deployment concern.

## SAFETY

Lease safety: PASS (bounded transient retry, watchdog, expiry/token checks retained).
Crash recovery: PASS (claim crash and running crash via lease-expiration fixtures; no stuck active job).
Concurrent claim: PASS (four distinct jobs/four simultaneous Workers and original single-job/four-Worker contention).
Result idempotency: PASS (lost-response replay reuses evaluation, no repeat storage writes).
Worker authentication: PASS (hashed token, wrong Worker result ownership rejected, no secrets in API records).
Identity restart: PASS (existing tests confirm same ID/token and first enrollment only).
Idle staggering: PASS (independent deterministic-seed scheduler tests and four-worker virtual-time model; not physical Mac timing).
Dashboard/WebSocket: PASS (original phase-event and auth tests; periodic refresh retained, frontend typecheck/build).

## TESTS

Final full suite: `.venv/bin/python scripts/test_integration.py --tb=short`.
Uses disposable PostgreSQL schemas with migrations, existing Redis test DB, trusted fixtures only. Does not execute uploaded agents or benchmarks. Final result: **61 passed, 1 skipped** in 9.44 seconds; the optional live NIFUS test is skipped when its explicit opt-in is absent.

Simulation: `.venv/bin/python -m scripts.simulate_worker_load > docs/load-simulation.json`.
SQL/transaction assertions: tests/test_optimization.py, including actual SELECT/UPDATE/INSERT/COMMIT counts; SQLAlchemy engine events do not count pre-ping or DBAPI BEGIN/ROLLBACK wire messages.
Storage: real boto3 serialization and Stubber (no live S3/R2), plus API recreation with independent in-memory object backend. Migration 003 applied in all integration schemas.

## LINT / TYPE CHECK

`.venv/bin/ruff check apps/api workers scripts tests` — PASS.
`npm run typecheck --prefix apps/web` — PASS.
`npm run build --prefix apps/web` — PASS.
`UV_CACHE_DIR=/tmp/abx-uv-cache uv pip check --python .venv/bin/python` — PASS.
No mypy or new testing toolchain introduced. Existing FastAPI/Starlette deprecation warnings remain. Initial sandbox-restricted DB and package network access failed; authorized network-enabled runs succeeded. Project .venv has no pip; dependency installation used the existing uv tool.

## FINAL RECOMMENDATION

The implemented normal-path communication budget is appropriate for 600 worker-hours/month and the stated 0/600/3000/6000 job scenarios: even high Redis usage (66,240) has substantial command headroom. HTTP/SQL monthly figures for job scenarios are explicitly fixture-dependent; actual runtime, test counts, bandwidth and cloud latency remain UNKNOWN. Finish private durable storage configuration, migration/deployment and one real Mac HTTPS evaluation before calling this production verified. No real Mac/cloud success is claimed.
