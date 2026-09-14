# AgentBenchX

A working Phase 1 control plane for reproducible AI coding-agent evaluation. Upload immutable agents, discover problems from external Git repositories, queue submissions, coordinate generic Mac workers, and inspect results, metrics and failure artifacts.

**Uploaded agents and benchmark code never execute on the central server.** The Mac-only Docker worker is implemented; real Mac #1 verification requires access to that machine. See the [Mac runbook](docs/mac-worker-1.md) and [Phase 2 status](docs/phase2-verification.md).

## Run

```bash
cp .env.example .env
# Set ADMIN_TOKEN to a random secret. Trusted workers register automatically.
docker compose up --build -d
```

Open **http://localhost:3000** and connect using ADMIN_TOKEN from your .env. API documentation: http://localhost:8000/docs. Configure server-side SSH before syncing NIFUS Bench. The default launch registers the source without requiring Git credentials or executing tasks.

The included native-development ports are PostgreSQL 55432 and Redis 56379. See [development instructions](docs/development.md) for Python/Node setup, Windows paths, configuration, SSH-agent forwarding and tests.

```text
apps/api/agentbenchx/   FastAPI, models, typed contracts and services
apps/web/              Next.js, TypeScript and Tailwind dashboard
workers/evaluator/     Mac worker loop, Docker sandbox, leases and protocol client
packages/schemas/      Portable OpenAPI contract
problem_sources/       Adapter extension guidance (not benchmark source code)
storage/               Ignored source caches, uploads and evaluation artifacts
infrastructure/        Dockerfiles and optional SSH-agent Compose override
migrations/            Alembic schema and immutability guards
tests/                 Unit, PostgreSQL/Redis and opt-in NIFUS tests
docs/                  Architecture, protocols and verification report
scripts/               Source seed, contract export and integration runner
```

The stack is FastAPI/Pydantic/SQLAlchemy/Alembic with PostgreSQL and Redis; Next.js/React/TypeScript/Tailwind provides the UI. PostgreSQL atomically assigns jobs; Redis distributes queue notifications and WebSocket events. A durable database queue fallback preserves progress through Redis failures.

Real NIFUS-bench inspection at `55014c032f9532834cccc780f4d372c90ebcb37f` discovered **29 supported problems (21 coding, 8 database)** and excluded **17 GIS tasks**. Tasks use Harbor `task.toml`; the source is cloned into a UUID cache, not vendored into the application. Every ProblemVersion stores repository URL, branch, full commit and path.

Dashboard routes: `/dashboard`, `/agents`, `/agents/[id]`, `/problem-sources`, `/problems`, `/problems/[id]`, `/workers`, `/evaluations`, `/evaluations/[id]`, `/evaluations/[id]/failure-analysis`. UI displays persisted data and actionable empty states, never fabricated execution results.

See [architecture](docs/architecture.md), [problem sources](docs/problem-sources.md), [worker protocol](docs/worker-protocol.md), [evaluation flow](docs/evaluation-flow.md), [security boundaries](docs/security.md), and [engineering verification report](docs/verification.md).

Phase 2: **Mac Worker #1 → end-to-end evaluation → verify result/cost/runtime/failure artifacts → Mac #2 → Mac #3 → Mac #4.** Every worker uses the same implementation and dynamic job queue.

Trusted workers automatically register on first connection to their existing SERVER_URL and retain a private identity for reconnects. No manual worker approval, registration token, static IP, VPN changes, or SSH runtime dependency is required. See [the worker runbook](docs/mac-worker-1.md).

## Worker communication and production storage

See [optimization report and deployment steps](docs/communication-optimization.md) and [reproducible load simulation](docs/load-simulation.json). Apply migration `003_job_event` before deploying this API revision. Existing endpoints, worker credentials and local storage remain supported.
