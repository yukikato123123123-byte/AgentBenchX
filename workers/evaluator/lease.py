import threading
import time

import httpx


class LeaseKeeper:
    """Independent renewal thread and deadline watchdog; stop work before server lease expiry."""

    def __init__(self, client, worker_id: str, job: dict, interval: float = 20):
        self.client, self.worker_id, self.job = client, worker_id, job
        self.interval = min(interval, job["lease_seconds"] / 4)
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.deadline = time.monotonic() + job["lease_seconds"] * 0.75
        self.lock = threading.Lock()
        self.event = "job.started"
        self.threads = []

    def renew(self, event: str | None = None):
        with self.lock:
            if self.cancel.is_set():
                raise RuntimeError("Lease no longer valid")
            if event:
                self.event = event
            while not self.cancel.is_set():
                sent_at = time.monotonic()
                if sent_at >= self.deadline:
                    self.cancel.set()
                    raise RuntimeError("Lease renewal deadline exceeded")
                try:
                    self.client.job_heartbeat(
                        self.worker_id, self.job["id"], self.job["lease_token"], self.event
                    )
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                        self.cancel.set()
                        raise
                    # Retry transient failures only within the last acknowledged safety window.
                    # A watchdog can cancel even while HTTP is blocked. Never extend on failure.
                    self.cancel.wait(min(2, max(0, self.deadline - time.monotonic())))
                    continue
                if self.cancel.is_set() or time.monotonic() >= self.deadline:
                    self.cancel.set()
                    raise RuntimeError("Lease acknowledgment arrived after safety deadline")
                self.deadline = sent_at + self.job["lease_seconds"] * 0.75
                return
            raise RuntimeError("Lease no longer valid")

    def __enter__(self):
        self.renew()

        def renew_loop():
            while not self.done.wait(self.interval):
                try:
                    self.renew()
                except Exception:
                    # Stop immediately on failed renewal, even before the conservative deadline.
                    self.cancel.set()
                    return

        def watchdog():
            while not self.done.wait(0.1):
                if time.monotonic() >= self.deadline:
                    self.cancel.set()
                    return

        self.threads = [threading.Thread(target=f, daemon=True) for f in (renew_loop, watchdog)]
        for thread in self.threads:
            thread.start()
        return self

    def __exit__(self, *args):
        self.done.set()
        for thread in self.threads:
            thread.join(timeout=12)
