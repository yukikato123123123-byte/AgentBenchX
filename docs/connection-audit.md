# Existing connection audit — 2026-09-14

Historical audit before the trusted-network registration update. The endpoint/registration observations below are point-in-time evidence. Current first-time registration requires no bootstrap token; see [phase2-verification.md](phase2-verification.md).

No network, VPN, forwarding, address, server environment, client configuration, credential, or registration was changed. Only documentation was corrected, and local verification artifacts were generated. The earlier SSH-forwarding suggestion and requirement to supply SSH details have been removed from the Mac runbook and Phase 2 report.

## Existing endpoint evidence

- Running API: `http://127.0.0.1:8000`; `GET /api/v1/health` returned 200 with database and Redis healthy.
- Dashboard: `http://localhost:3000`; `/dashboard` returned 200. The running standalone frontend uses HOSTNAME=127.0.0.1, PORT=3000. Its built `/api/:path*` rewrite targets `http://127.0.0.1:8000/api/:path*`.
- Root `.env`: WEB_ORIGIN=http://localhost:3000 and API_INTERNAL_URL=http://api:8000. The latter is the existing Compose-internal API service name, not a discovered Mac-facing URL. The native frontend uses its built localhost rewrite.
- Compose publishes the existing API/web ports on loopback. These localhost addresses describe access on the server; they do not establish what endpoint an existing Mac installation uses. No external endpoint or network route was inferred or created.
- Configured PostgreSQL is localhost:55432/agentbenchx; Redis is localhost:56379/0. Credentials were inspected only for presence and never printed.

## Registration and connection mechanism

`apps/api/agentbenchx/models.py` defines the existing workers table; there is no separate clients table in this configured database. Worker metadata contains UUID, name, hostname, platform, status, version, heartbeat, capabilities, and a token hash. Hostname is descriptive metadata, not an SSH transport or a stored server URL.

The Mac initiates outbound HTTP requests using `workers/evaluator/client.py`. Initial registration uses `POST /api/v1/workers/register` with the dedicated bootstrap token and receives a unique UUID/token. Heartbeat, atomic claim, lease renewal, immutable agent download, and result upload all use the existing authenticated API. The server does not initiate a connection to the Mac.

`workers/evaluator/config.py` reads `.env.worker` relative to launch working directory, with environment-variable overrides. SERVER_URL is required. `workers/evaluator/main.py` reuses either WORKER_ID/WORKER_TOKEN or `WORKSPACE_ROOT/identity.json`, which stores id, token and server_url. A saved identity must match the configured URL. Existing settings and identities should be preserved.

SSH is optional installation/diagnostic access only, not a worker-protocol prerequisite. The operator can perform those steps locally on the Mac. No SSH password, private key, or token is requested.

## Mac #1 status and search scope

Live `GET /api/v1/workers` returned `[]`. Direct read-only SQL against the configured database confirmed **0 workers, 0 runner jobs, 0 evaluations, and 29 problems**. Therefore this database has no current Mac #1 registration, and no real Mac evaluation is verified.

The project configuration scan included ignored environment files while excluding dependency/build directories and benchmark repository caches. The only environment files found in AgentBenchX were root `.env`, root `.env.example`, and `workers/evaluator/.env.example`. No deployed `.env.worker` or `identity.json` was found. The current host's default `~/Library/Application Support/AgentBenchX/identity.json` does not exist. No worker configuration variables were present in the audit process environment; the running API/web environment checks found no Mac worker settings.

The workspace was also searched for `.env.worker`, `identity.json`, launchd/setup files, and AgentBenchX/Mac #1/worker-setting references in sibling projects and the workspace `.agents`/`.codex` directories. No existing AgentBenchX Mac installation or registration source was identified there. The worker example's localhost URL and Mac label are templates, not registration evidence.

This audit cannot inspect files stored only on the actual Mac, another deployment, or an external client manager. It does not establish that such an existing configuration is absent everywhere.

## Local verification rerun

- 48 tests passed, including original Phase 1 tests, PostgreSQL disposable-schema migrations, Redis, atomic claims, result/artifact round trips, worker authentication/leases, trusted timeout/recovery fixtures, events, and cached NIFUS metadata inspection. Two upstream deprecation warnings remain.
- Alembic check: no new upgrade operations detected.
- Ruff and TypeScript: passed.
- Production frontend build: passed in `/tmp/agentbenchx-web-audit-izxr4jtq/web`, an isolated source copy using installed dependencies. Initial restricted subprocess execution failed; the same build passed with normal subprocess access. The running frontend build was not replaced or restarted.
- Browser: agents, problem sources, 29-problem library and problem detail, workers, evaluations, mobile layout without horizontal overflow, and live-event connection passed. No JavaScript errors. Total cost, average cost and total tokens displayed N/A; the live API returned null for those metrics.
- Existing API, PostgreSQL and Redis health passed. Phase 1's previously documented API/web Docker image package-download limitation remains; no networking was changed to bypass it.

These tests use trusted fixtures and mocked worker Docker calls. No uploaded agent or benchmark test code executed on the server, and no real Mac execution, runtime, cost, patch, failure artifacts, timeout or recovery result is claimed.

## What works and exact next action

The existing control plane and outbound worker protocol are available without redesign. A Mac with its existing working SERVER_URL and identity can use that path unchanged. Reachability from the actual Mac has not been measured here.

The genuinely missing evidence is the location/content of Mac #1's existing launch configuration (its non-secret SERVER_URL and identity source), followed by an actual worker connection and evaluation receipts. The server's empty registry and localhost configuration cannot recover those Mac-local settings.

Next action for the user: identify where Mac #1's existing AgentBenchX installation/configuration is managed—provide only its location, not credentials or the full secret-bearing file. Then inspect/reuse that configuration and start the existing worker. Once it connects, run the existing `scripts/phase2_validate.py` sequence for the single selected problem. Do not move to Mac #2 until actual success/failure/timeout/recovery evidence is complete.
