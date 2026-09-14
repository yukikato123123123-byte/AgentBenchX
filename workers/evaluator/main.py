"""Run with python -m workers.evaluator --once on Mac Worker #1."""

import argparse
import fcntl
import json
import logging
import os
import platform
import signal
import threading
import time
from uuid import UUID

import httpx

from .client import WorkerClient
from .config import WorkerSettings
from .docker_sandbox import CleanupError, DockerSandbox
from .evaluator import Evaluator
from .lease import LeaseKeeper
from .scheduling import IdleSchedule

log = logging.getLogger(__name__)
VERSION = "0.2.0"


def identity(settings: WorkerSettings) -> tuple[str, WorkerClient]:
    path = settings.workspace_root / "identity.json"
    if settings.worker_id:
        return str(UUID(settings.worker_id)), WorkerClient(
            settings.server_url, settings.worker_token.get_secret_value()
        )
    if path.exists():
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("Worker identity must be a private regular file (chmod 600)")
        stored = json.loads(path.read_text())
        if stored["server_url"] != settings.server_url:
            raise ValueError("Saved identity belongs to another SERVER_URL")
        return str(UUID(stored["id"])), WorkerClient(settings.server_url, stored["token"])
    client = WorkerClient(settings.server_url)
    credentials = client.register(
        name=settings.worker_name,
        hostname=platform.node(),
        platform=platform.system().lower(),
        version=VERSION,
        capabilities=["harbor", "docker", settings.docker_platform],
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(
            {"id": credentials["id"], "token": credentials["token"], "server_url": settings.server_url},
            stream,
        )
    return credentials["id"], client


def recover_sandboxes(settings: WorkerSettings) -> None:
    """After a crash, remove only this workspace's UUID-named job containers before claiming work."""
    for folder in settings.workspace_root.glob("evaluations/*/workspace"):
        job_id = str(UUID(folder.parent.name))
        sandbox = DockerSandbox(settings, threading.Event())
        sandbox.containers = {f"abx-{job_id}-agent", f"abx-{job_id}-test"}
        sandbox.image = f"agentbenchx-job:{job_id}"
        sandbox.destroy(job_id)


def replay_pending(settings, worker_id, client) -> None:
    for path in settings.workspace_root.glob("evaluations/*/result.pending.json"):
        result = json.loads(path.read_text())
        job_id = str(UUID(path.parent.name))
        try:
            receipt = client.result(worker_id, job_id, result)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {403, 404, 409}:
                path.rename(path.with_name("result.rejected.json"))
                continue
            raise
        (path.parent / "receipt.json").write_text(json.dumps(receipt, indent=2))
        path.rename(path.with_name("result.accepted.json"))


def run_worker(settings: WorkerSettings, once: bool = False) -> None:
    # Enforced even for `--once`; no Linux/Windows evaluation switch is exposed.
    DockerSandbox(settings, threading.Event()).preflight()
    settings.workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.workspace_root.chmod(0o700)
    lock = (settings.workspace_root / "worker.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    stop = threading.Event()
    active_lease = None

    def shutdown(*_):
        stop.set()
        if active_lease:
            active_lease.cancel.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, shutdown)
    worker_id, client = identity(settings)
    try:
        recover_sandboxes(settings)
        replay_pending(settings, worker_id, client)
        schedule = IdleSchedule(settings)
        if not once:
            stop.wait(schedule.startup_delay())
        while not stop.is_set():
            try:
                replay_pending(settings, worker_id, client)
                current = time.monotonic()
                if schedule.heartbeat_due(current):
                    client.heartbeat(worker_id)
                    schedule.heartbeat_sent(time.monotonic())
                if not schedule.poll_due(time.monotonic()):
                    stop.wait(schedule.wait_seconds(time.monotonic()))
                    continue
                job = client.claim(worker_id)
                if job is None:
                    if once:
                        return
                    schedule.empty(time.monotonic())
                    stop.wait(schedule.wait_seconds(time.monotonic()))
                    continue
                schedule.found()
                log.info("Claimed job %s", job["id"])
                with LeaseKeeper(client, worker_id, job, settings.worker_heartbeat_interval) as lease:
                    active_lease = lease
                    evaluator = Evaluator(settings, client)
                    result, folder = evaluator.evaluate(job, worker_id, lease)
                    if lease.cancel.is_set():
                        log.warning("Lease lost; local evidence preserved for job %s", job["id"])
                    else:
                        # Keep heartbeats alive while retrying transient result-upload failures.
                        for attempt in range(3):
                            try:
                                receipt = client.result(worker_id, job["id"], result)
                                evaluator.acknowledged(folder, receipt)
                                log.info("Job %s acknowledged as %s", job["id"], receipt["status"])
                                break
                            except httpx.TransportError:
                                if lease.cancel.wait(2**attempt):
                                    break
                        else:
                            stop.set()  # Restart replays pending result before claiming anything else.
                active_lease = None
                schedule.heartbeat_sent(time.monotonic())
                if once:
                    return
            except CleanupError:
                log.error("Sandbox cleanup could not be confirmed; worker stopped")
                stop.set()
                client.heartbeat(worker_id, "ERROR")
            except (httpx.HTTPError, OSError, ValueError, RuntimeError):
                # No raw exception/HTTP headers/token values in operator logs.
                log.error("Worker operation failed; credentials omitted, evidence retained")
                if once:
                    raise RuntimeError("Worker operation failed") from None
                stop.wait(settings.worker_poll_interval)
    finally:
        try:
            client.heartbeat(worker_id, "OFFLINE")
        except httpx.HTTPError:
            pass  # Server reaper provides eventual OFFLINE if shutdown delivery is impossible.
        client.close()
        lock.close()


def main():
    parser = argparse.ArgumentParser(description="AgentBenchX Mac Worker #1")
    parser.add_argument("--once", action="store_true", help="Process at most one job and stop")
    parser.add_argument(
        "--check", action="store_true", help="Verify local Mac/Docker prerequisites without registering"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = WorkerSettings()
    if args.check:
        DockerSandbox(settings, threading.Event()).preflight()
        return
    run_worker(settings, once=args.once)
