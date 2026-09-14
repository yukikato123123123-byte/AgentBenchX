# Development and verification

Use Python 3.12+, Node 22+, Git and Docker Compose. Next.js App Router setup follows the [official installation guide](https://nextjs.org/docs/app/getting-started/installation). This project uses the Webpack builder because Turbopack hit a subprocess/socket restriction in the development environment. Dependency versions are captured in requirements.lock and apps/web/package-lock.json.

From the AgentBenchX directory:

```bash
cp .env.example .env
# Replace the two token values with separate random secrets.
docker compose up --build -d
```

Visit http://localhost:3000, enter ADMIN_TOKEN from your local .env, and open Problem sources. Click Sync source once server-side SSH access is configured. Open Agents, create an agent, upload agent.py, then Run evaluation and select problems. Jobs will remain queued until a worker claims them. No sample success results or fake workers are inserted.

The existing development .env was generated with random credentials and is ignored by Git. Never paste these credentials into issue reports. The API health route is http://localhost:8000/api/v1/health; OpenAPI documentation is http://localhost:8000/docs. Generated portable API schema: packages/schemas/openapi.json.

Environment variables:

| Variable | Purpose |
|---|---|
| POSTGRES_USER / PASSWORD / DB | Compose PostgreSQL initialization |
| DATABASE_URL | SQLAlchemy PostgreSQL connection (native API) |
| REDIS_URL | Queue/event Redis connection |
| STORAGE_ROOT | Upload, source-cache and artifact base directory |
| ADMIN_TOKEN | Administrator REST/WebSocket authentication |
| SERVER_URL | Existing server origin in worker configuration; first connection registers automatically |
| WEB_ORIGIN | Allowed browser origin (default http://localhost:3000) |
| GIT_ALLOWED_HOSTS | JSON array of allowed Git hosts |
| GIT_SSH_COMMAND | Optional operator-managed SSH command |
| API_INTERNAL_URL | Next.js reverse-proxy target; baked into build rewrites |
| LEASE_SECONDS / MAX_ATTEMPTS | Default 120 seconds / 3 attempts |

For native API and web development, Compose exposes PostgreSQL on localhost:55432 and Redis on localhost:56379. Use these ports in .env when running outside containers (Compose overrides both inside the API container):

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
npm ci --prefix apps/web
docker compose up -d postgres redis
.venv/bin/alembic upgrade head
.venv/bin/python scripts/seed.py
.venv/bin/uvicorn agentbenchx.main:app --host 127.0.0.1 --port 8000
# Separate terminal:
npm run dev --prefix apps/web
```

On PowerShell use `.venv\Scripts\python.exe`, `pip.exe`, `alembic.exe` and `uvicorn.exe` in place of `.venv/bin/*`. Docker Desktop must use Linux containers. Makefile targets are convenience wrappers for Unix shells, not a Windows requirement.

Tests:

```bash
.venv/bin/pytest -q                     # Unit tests; integration tests skip without explicit DB
.venv/bin/python scripts/test_integration.py # Disposable PostgreSQL schemas + real Redis
NIFUS_REPOSITORY=storage/problem_sources/11111111-1111-4111-8111-111111111111/repository .venv/bin/python scripts/test_integration.py
.venv/bin/ruff check apps/api workers scripts tests
npm run typecheck --prefix apps/web
npm run build --prefix apps/web
.venv/bin/python scripts/export_schemas.py
```

Integration tests create random `test_<uuid>` schemas, apply actual Alembic migrations, and drop only their own schemas. They never truncate application data. Unit Git tests use mocked responses and a temporary local Git fixture. Real NIFUS integration is opt-in and only reads metadata. The PostgreSQL URL must be a development database whose account can create/drop schemas.

Alembic migrations include explicit table/index definitions and database-level version immutability triggers. Do not use ORM create_all for application startup. To add a migration, use `alembic revision --autogenerate -m ...`, review it, then run `alembic upgrade head` and integration tests. Keep generated OpenAPI in sync after API changes.

SSH in Linux/WSL Docker can use:

```bash
export SSH_KNOWN_HOSTS="$HOME/.ssh/known_hosts"
docker compose -f docker-compose.yml -f infrastructure/ssh-agent.compose.yml up -d api
```

This requires SSH_AUTH_SOCK to refer to an already authorized agent. Windows-native agent forwarding depends on the chosen Docker Desktop transport; use an operator-managed Linux/WSL agent or run the API natively with Windows OpenSSH. Do not copy private keys into an image.

Environment-specific build note: verification here found Docker Debian package downloads refused on both HTTP and HTTPS, including with the optional `infrastructure/host-build.compose.yml` build-network override. PostgreSQL and Redis images started successfully. Until Docker egress is repaired, run the API and production web natively against those containers; this does not remove or weaken the control-plane execution boundary.
