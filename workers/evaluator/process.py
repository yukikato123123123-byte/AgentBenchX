"""Bounded process groups for trusted orchestration commands, not a host agent runner."""

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Outcome:
    exit_code: int
    stdout: str
    stderr: str
    started_at: str
    finished_at: str
    runtime_seconds: float
    timed_out: bool = False
    cancelled: bool = False


def run(
    argv: list[str],
    timeout: float,
    cancel: threading.Event | None = None,
    cwd: Path | None = None,
    limit: int = 500_000,
) -> Outcome:
    """Drain both pipes continuously with a memory cap; kill descendants on every exit."""
    start = time.monotonic()
    started = datetime.now(timezone.utc).isoformat()
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    buffers = [bytearray(), bytearray()]
    truncated = [False, False]

    def drain(stream, index):
        while chunk := stream.read(8192):
            room = max(0, limit - len(buffers[index]))
            buffers[index].extend(chunk[:room])
            truncated[index] |= len(chunk) > room
        stream.close()

    readers = [
        threading.Thread(target=drain, args=(stream, i), daemon=True)
        for i, stream in enumerate((process.stdout, process.stderr))
    ]
    for reader in readers:
        reader.start()
    timed_out = cancelled = False
    try:
        while process.poll() is None:
            cancelled = bool(cancel and cancel.is_set())
            timed_out = time.monotonic() - start >= timeout
            if cancelled or timed_out:
                break
            time.sleep(0.05)
    finally:
        # A parent can exit while grandchildren keep running or hold the pipes open.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=5)
    texts = [
        b.decode("utf-8", errors="replace") + ("\n[output truncated]" if truncated[i] else "")
        for i, b in enumerate(buffers)
    ]
    return Outcome(
        process.returncode,
        *texts,
        started,
        datetime.now(timezone.utc).isoformat(),
        time.monotonic() - start,
        timed_out,
        cancelled,
    )
