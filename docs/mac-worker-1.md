# Mac Worker #1 runbook

This adds a worker to the existing Phase 1 platform. It reuses registration, per-worker bearer authentication, lease-bound agent download, atomic claims, job heartbeats, result ingestion, artifacts, Redis events and the dashboard. It does not replace the database or queue. Actual execution is blocked on non-Darwin hosts and remote Docker contexts.

## Prerequisites on Mac #1

- Python 3.12+, Git, and Docker Desktop with Linux containers running.
- A local Unix-socket Docker context; remote Docker daemons are rejected to prevent accidentally running jobs on the central server.
- Read access to the exact NIFUS-bench repository using **Mac-owned**, least-privilege credentials and verified known_hosts. Never copy the control plane's SSH private key.
- Reuse the existing Mac worker SERVER_URL and connection path. Inspect its launch environment, existing `.env.worker`, and saved identity before installation. Preserve existing addresses, VPN, forwarding, and client settings. The example URL is a template, not evidence of an installed Mac configuration. See [connection-audit.md](connection-audit.md) for the server-side findings.
- Docker Desktop disk/memory limits suitable for a disposable benchmark build. The selected source requests 3072 MB memory. Its base image is pinned; further package downloads must be reachable. Use `linux/amd64` on Apple Silicon for the initial verifier compatibility check.

If the worker software is not installed, copy project source to the Mac **excluding .env, .venv, storage, node_modules, .next, and all SSH material**. Keep apps/api/agentbenchx (shared Git abstraction), workers/, and pyproject.toml. Run from the project root:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .
```

No manual worker registration or approval is required. On first connection, the worker calls the existing registration endpoint without a bootstrap credential and saves the server-issued UUID/token in its private identity file. Subsequent starts reuse that identity automatically. Mac #1 follows exactly the same registration flow as other trusted clients.

Use the existing `.env.worker` or launch environment without overwriting it. SERVER_URL is the only required connection setting; reuse the existing server origin. WORKER_NAME and WORKSPACE_ROOT are optional. The server-local URL in the template is not evidence of the endpoint configured on an actual Mac. No client static IP, port forwarding, VPN reconfiguration, or new network path is required by this flow.

Do not manually copy or register worker IDs. Existing WORKER_ID/WORKER_TOKEN configurations remain supported, as do existing saved identities. Legacy WORKER_REGISTRATION_TOKEN entries are ignored and need not be removed from existing environment files. Administrator authentication, per-worker authentication after registration, and job leases remain enforced.

SSH is not part of runtime worker registration. It is only an optional administrative tool for installation or diagnostics; local installation on the Mac works equally well. No passwords, private keys, or tokens should be sent in chat.

```bash
.venv/bin/python -m workers.evaluator --check
.venv/bin/python -m workers.evaluator
# Or process at most one available job, then stop:
.venv/bin/python -m workers.evaluator --once
```

Without a saved identity, the worker registers once and writes `WORKSPACE_ROOT/identity.json` with mode 600, storing the server-issued UUID and individual token. Subsequent starts reuse it. Existing installations may instead supply both WORKER_ID (the UUID issued by the existing API) and WORKER_TOKEN. Arbitrary names such as mac-01 are not database IDs; use WORKER_NAME for human-readable naming.

Configuration: SERVER_URL, WORKER_ID, WORKER_NAME, WORKER_TOKEN, WORKER_POLL_INTERVAL (5s), WORKER_HEARTBEAT_INTERVAL (20s), WORKSPACE_ROOT, GIT_ALLOWED_HOSTS, DOCKER_PLATFORM (linux/amd64), SANDBOX_CPUS (2), SANDBOX_MEMORY_MB (3072), BUILD_TIMEOUT_SECONDS (1800). Never print credential values in diagnostics.

## Execution and cleanup

Each claimed job gets `WORKSPACE_ROOT/evaluations/<job-id>/workspace/`. The server does not allocate an evaluation ID until it accepts a result, so the unique RunnerJob ID is the correct workspace key. Existing workspaces are never reused. Paths reject traversal and resolved symlink escapes.

The worker downloads agent.py from the existing lease-bound endpoint and verifies filename, version, byte size and SHA-256. It reuses GitRepositoryManager to clone a Mac-local bare cache, resolve the assigned SHA, and copy regular task blobs. It does not use a branch tip. Reference solution files are excluded from extracted task material; the bare cache is outside every container mount.

Only `environment/` is sent as the Docker build context. The resulting image ID is captured. Agent execution uses the image's repository WORKDIR, the unprivileged `agent` user, no network, no added capabilities, bounded CPU/memory/PIDs/file size, no Docker socket, no host credential mounts, and a read-only harness/input mount. The ABI is `agent_main({instruction, workspace, job_id}) -> unified_diff`. Direct edits are also collected with Git if the agent returns an empty string.

The agent container is stopped before verification. A **fresh** container from the same image receives only the patch, trusted harness and read-only benchmark tests. It checks/applies the patch, preserves `/logs/agent/patch.diff`, and executes the ProblemVersion's exact argv. The verifier gets DAC_OVERRIDE inside the container so it can reset the baked agent-owned repository; it gets no host write mounts or network. Result counts come from actual `/logs/verifier/junit.xml`; missing/invalid JUnit is an infrastructure failure, never fabricated pass counts. A failed verifier exit or reward zero cannot become a success even if JUnit tests passed.

This first adapter supports Harbor images with an `agent` user, a repository WORKDIR, Python/Git, and JUnit at the standard verifier path. It does not claim every future Harbor variant is already executable. Other report formats need adapters; they must not be guessed from console output. Dependencies that need runtime networking require a separately reviewed policy, not silent host/network fallback.

The ProblemVersion timeout applies independently to agent and verifier phases. The image build has its own configured timeout. The default lease is 120 seconds; renewal runs separately throughout input preparation, build, execution, collection and result upload. A deadline watchdog and failed-renewal detection cancel execution conservatively before expiry. Docker containers are explicitly killed and their stopped state checked; killing the CLI alone is not treated as sufficient. Cleanup failure stops the worker rather than letting it claim another job.

SIGINT/SIGTERM cancel the active job and clean containers. After an abrupt process death, startup cleans only recorded job UUID containers before claiming work. Local result evidence is saved with mode 600 before cleanup. The worker retries lost result responses using the existing idempotent endpoint; unacknowledged results are replayed after restart. Rejected stale results are retained as result.rejected.json. Accepted results retain result.accepted.json and receipt.json, and only then is the temporary workspace removed. Local pending files contain the job lease token and must remain private.

The worker returns ONLINE between jobs. Graceful stop reports OFFLINE; missed heartbeats are reaped to OFFLINE and now emit a worker.status event. No Mac #2–#4 processes are added by these instructions.

## One-problem controlled verification

The exact persisted selection is [phase2-problem.json](phase2-problem.json):

- Problem: `41966f8e-be97-4001-8f95-a046e9ad11d8`
- Version: `8e2be30a-7170-4f0f-bfb4-feb5e9e3b376`
- Commit: `55014c032f9532834cccc780f4d372c90ebcb37f`
- Path: `13_08_2026/joserfc__flattened-signature-validation`
- Category: CODING; test command: `bash /tests/test.sh`; timeout: 600 seconds.

Three controlled agents are supplied under workers/evaluator/validation_agents. The success agent fixes the unsafe assertion exactly as requested by the instruction, returning a normal unified diff. The failure agent leaves the source unchanged, which should fail the actual verifier. The timeout agent sleeps indefinitely. They are infrastructure checks, not a general coding-agent benchmark or LLM implementation. No test oracle or expected answer is changed.

Start only Mac #1 in continuous mode. On the **central server**, run:

```bash
.venv/bin/python scripts/phase2_validate.py
```

The driver only uploads bytes and calls the API; it never imports or executes agent.py or benchmark tests. It refuses to submit unless exactly one eligible Mac worker is ONLINE, verifies the stored problem commit, captures WebSocket events, and queues success → failure → timeout → success/recovery sequentially. It never submits the 29-problem library. If the first success does not pass, subsequent stages do not run. The 600-second timeout is preserved; it is not shortened by mutating the immutable ProblemVersion.

Evidence goes under storage/phase2-verification/<timestamp>/, including exact inputs, worker metadata, full evaluation traceability, six artifact files per stage and events.jsonl. VERIFIED.json is written only after every expected result and validation passes. Inspect the live dashboard during the run and retain browser evidence separately.

Token and cost values remain NULL/N/A for these agents, which expose no metered usage. No zero cost is inferred from the absence of an LLM call. Runtime, process exit/timestamps, tests and patch counts are measured. New execution evidence is stored in the existing Evaluation.metrics JSON; no database migration was needed.

Docker behavior follows the official [run options](https://docs.docker.com/reference/cli/docker/container/run/) and [container copy](https://docs.docker.com/reference/cli/docker/container/cp/) contracts. Copying artifacts reads a bounded tar stream and accepts only a single regular file; container paths are never extracted onto the Mac filesystem.

## Gate for Mac #2

Do not add Mac #2 until the actual Mac #1 success, controlled failure, timeout and recovery run has receipts and artifacts. Then provision Mac #2 with the same worker source and its own server-issued identity, and run a separate two-worker concurrency verification. This task does not start or configure it.
