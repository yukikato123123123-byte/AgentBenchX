# Phase 2 implementation and verification status

Date: 2026-09-14. **Worker implementation is present; the real Mac #1 end-to-end gate is not yet satisfied.** The connection audit found zero workers in the configured AgentBenchX database and no deployed Mac configuration in the accessible workspace. SSH is not required by the worker protocol; the earlier request for SSH details was premature. See [connection-audit.md](connection-audit.md). No Mac host is inferred from the Linux/WSL control-plane environment. No actual uploaded agent or benchmark verifier was run here.

## Changes and evidence

| Requested area | Delivered / current evidence |
|---|---|
| 1. Phase 1 reuse | Existing models, PostgreSQL, Redis queue, authentication, atomic claims, leases, download/result endpoints, artifact service and frontend retained. No schema redesign or new database tables. |
| 2. Mac #1 implementation | `workers/evaluator/main.py`, CLI, DockerSandbox, input verification, process management, lease keeper and evaluator. Real execution explicitly refuses non-Darwin hosts and remote Docker endpoints. |
| 3. Configuration | `.env.worker` with SERVER_URL, worker name/credentials, polling/heartbeat intervals, workspace and Docker resource settings. Example and Mac setup in `mac-worker-1.md`. |
| 4. Authentication | Automatic trusted-network registration without a bootstrap token → unique per-worker token, mode-600 saved identity, reuse after restart, no credential values in logs. |
| 5. Claims | Existing atomic server claims; one worker loop handles one job. Original simultaneous-claim tests retained and passing. |
| 6. Isolation | Unique UUID job workspaces, regular-blob extraction, solution exclusion from containers, immutable agent hash/size checks, separate agent/verifier containers with no network or privileged host mounts. |
| 7. Selected ProblemVersion | `8e2be30a-7170-4f0f-bfb4-feb5e9e3b376` for problem `41966f8e-be97-4001-8f95-a046e9ad11d8`, CODING, `13_08_2026/joserfc__flattened-signature-validation`. Read from the existing database; not fabricated. |
| 8. Commit | `55014c032f9532834cccc780f4d372c90ebcb37f`. Exact source identity saved in `phase2-problem.json`. |
| 9. Agent result | **Not measured on Mac.** Controlled legitimate success, no-op failure and timeout agents supplied; none executed on the control plane. |
| 10. Benchmark tests | **Not run on Mac.** Exact argv `bash /tests/test.sh`, real JUnit parser, verifier exit/reward checks implemented. No synthetic benchmark success claimed. |
| 11. Runtime | Process/runtime capture implemented; real evaluation runtime **unavailable** until Mac run. |
| 12. Tokens | Optional input/output/total tokens now preserve null rather than infer zero. Controlled agents expose no usage telemetry. |
| 13. Cost | Optional LLM/compute/total costs preserve null; dashboard shows N/A. Totals remain unavailable when components are missing. No financial values invented. |
| 14. Patch | Unified-diff return or collected direct edits, patch stats, separate-container replay, existing patch artifacts reused. Controlled success agent produces the instruction's assertion fix without modifying verifier expectations. Real patch result pending. |
| 15. Failure artifacts | Existing six files retained. analysis.md now includes actual submitted process evidence and server-owned worker/problem/version/commit/attempt context; root cause is explicitly not inferred. API integration verifies the round trip. |
| 16. Events/UI | Original WebSocket pipeline reused. Real Redis/WebSocket tests cover worker.status, job.started, agent.started/finished, test.started/finished and evaluation.completed. Missed heartbeats now emit OFFLINE events. New N/A rendering and live elapsed-runtime display implemented. Actual Mac dashboard evidence pending. |
| 17. Timeout | Trusted process-fixture test verifies real process-group termination and bounded output. Evaluator tests verify TIMEOUT payload and preserved evidence. The selected benchmark's actual 600-second timeout has **not** run on Mac. |
| 18. Recovery | Trusted fixtures verify failure → timeout → success in separate workspaces; cleanup failure stops the worker. Restart replays pending results and removes only recorded job containers. Actual Mac recovery pending. |
| 19. Automated tests | **50 passed**, including all prior Phase 1/2 tests and the automatic-registration integration tests, real PostgreSQL migrations/Redis and opt-in NIFUS metadata inspection. Two upstream test-client deprecation warnings. Ruff, TypeScript, production frontend build passed; Alembic reports no drift. |
| 20. Issues | No actual Mac worker is connected to this configured server; therefore Mac Docker image build, agent/verifier execution, real metrics, artifacts and dashboard state cannot be certified. Phase 1's Docker build-network limitation is not treated as resolved. |
| 21. Next step for Mac #2 | **Not authorized to proceed yet.** First obtain Mac #1 success/failure/timeout/recovery receipts and evidence using the runbook. Only then provision Mac #2 with the same software and its own identity. No Mac #2–#4 implementation/deployment performed. |

The complete 50-test command was:

```bash
NIFUS_REPOSITORY=storage/problem_sources/11111111-1111-4111-8111-111111111111/repository .venv/bin/python scripts/test_integration.py
.venv/bin/alembic check
```

Worker Docker calls were mocked in local tests. Only explicitly trusted process fixtures (small Python print/sleep commands) executed locally to verify timeouts and pipe handling; neither uploaded agents nor GitHub task/test code executed on the server. A Mac-only execution guard is tested before any Docker call.

## Minimal compatible API changes

- Nullable usage/cost fields and null-aware aggregates. Existing explicit numeric metrics still work. Historical evaluations are not rewritten.
- Optional `execution` evidence on the existing result payload, stored in the existing metrics JSON; no migration needed.
- Explicit OFFLINE heartbeat support, plus OFFLINE events from the existing reaper.
- Enriched existing failure report; no separate artifact protocol or AI diagnosis.
- Regenerated shared OpenAPI schema. Existing Phase 1 tests were retained.

## Remaining verification procedure

1. Locate Mac #1's existing installation/configuration and preserve its endpoint and identity. The server-side audit cannot read files residing only on that Mac. Share only the configuration location if assistance is needed, never passwords, keys, or tokens.
2. Follow [mac-worker-1.md](mac-worker-1.md) to check the existing Mac prerequisites and launch configuration; check the queue before starting the worker. No new network path or client reconfiguration is prescribed.
3. Start only Mac #1. Run the central `scripts/phase2_validate.py` driver. It submits only the single selected problem, sequentially: success → controlled test failure → timeout → success/recovery. The first failure stops the sequence.
4. Review the recorded events, input IDs, exact commit, process exit codes, JUnit cases, measured runtime, null usage/cost, patch and six artifact files. Capture the browser's actual worker/evaluation transitions.
5. Mark Mac #1 verified only with those real receipts; then consider Mac #2.

The driver writes `storage/phase2-verification/<timestamp>/VERIFIED.json` only after its required result checks pass. No such verification artifact or successful Mac evaluation is claimed in this report.

Live control-plane check after update: health HTTP 200; dashboard total_cost, average_cost and total_tokens all returned null; the database contained zero registered workers and zero evaluations. This confirms no Mac identity or successful evaluation was inserted to simulate completion. The original 29 synchronized problems remain available.

Browser verification passed: Total cost, Average cost and Total tokens displayed N/A; the live-event connection was established; Chromium reported no JavaScript errors. Screenshot: `storage/phase2-dashboard-null.png`. This is control-plane UI evidence, not a real Mac evaluation.


## Trusted-network automatic registration update

Previously, first startup registered automatically but required WORKER_REGISTRATION_TOKEN. Now anyone who can reach the existing private server can start a worker: the client calls the same registration endpoint without credentials, receives a unique UUID/token, saves its mode-600 identity, and begins heartbeat/polling. Restart reuses that identity without creating another registration. Mac #1 uses the same flow as other clients.

No manual worker registration, approval, copied worker IDs, static client IP, port forwarding, VPN reconfiguration, or SSH runtime registration is required. SERVER_URL remains the required existing server origin, supplied through the existing worker environment or `.env.worker`. Optional name/workspace settings retain their defaults. Existing configured IDs/tokens and saved identities remain compatible. Legacy bootstrap-token entries are ignored, so existing `.env` files need no edits.

The existing server-local API remains http://127.0.0.1:8000 and dashboard http://localhost:3000; the Compose-internal API URL remains http://api:8000. No Mac-facing endpoint is invented from those addresses. The worker's existing SERVER_URL must remain in use on the actual Mac.

Only admission changed. Administrator authentication, individual worker-token checks, lease authorization, Docker isolation, exact commits/checksums, timeout and lease-loss cancellation, cleanup/recovery, JUnit, patch/failure artifacts, null metrics, and controlled evaluation scenarios remain intact. No schema migration or networking changes are needed. Real Mac #1 success/failure/timeout/recovery evidence remains pending.


Validation after the trusted-registration change: **50 tests passed** (two upstream deprecation warnings). The added tests drive the real worker client against the API to verify first connection without bootstrap credentials, private persistent identity, restart without duplicate registration, independent workers, heartbeat, empty polling, exclusive job claim, valid lease renewal, cross-worker rejection, and invalid-lease rejection. Existing cancellation, recovery, exact-input and artifact tests remain passing. Anonymous administrator and subsequent worker operations remain rejected.

Ruff, TypeScript, Alembic schema consistency, and the production frontend build passed. The build used an isolated copy verified against current frontend source; no running dashboard artifacts were replaced. The API was reloaded on the same 127.0.0.1:8000 binding with its existing environment. Live health reports database/Redis healthy, and the live OpenAPI exactly matches the regenerated shared contract. Anonymous registration reaches input validation; no synthetic worker was inserted into the application database. Live workers remain empty and evaluations remain zero.

Post-reload browser checks passed for all dashboard pages, 29 problem rows and problem detail, mobile layout without horizontal overflow, live updates, and all three N/A token/cost cards. No JavaScript errors were reported. These are local control-plane checks, not Mac evaluation evidence.
