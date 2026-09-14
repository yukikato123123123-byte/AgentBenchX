"""Docker Desktop isolation on Mac only; no host or remote-daemon execution fallback."""

import io
import json
import os
import platform
import shutil
import subprocess
import tarfile
import threading
from pathlib import Path
from uuid import UUID

from .config import WorkerSettings
from .process import Outcome, run
from .sandbox import SandboxManager


class SandboxError(RuntimeError):
    pass


class CleanupError(SandboxError):
    """Fail closed: do not claim more work when local execution cannot be stopped."""


class DockerSandbox(SandboxManager):
    def __init__(self, settings: WorkerSettings, cancel: threading.Event):
        self.settings, self.cancel = settings, cancel
        self.workspace: Path | None = None
        self.image: str | None = None
        self.image_id: str | None = None
        self.containers: set[str] = set()
        self.job_id: str | None = None
        self.current: str | None = None

    def preflight(self) -> None:
        if platform.system() != "Darwin":
            raise SandboxError("Real evaluation is enabled only on a Mac worker; this host is not Darwin")
        configured_host = os.environ.get("DOCKER_HOST", "")
        if configured_host and not configured_host.startswith("unix://"):
            raise SandboxError("DOCKER_HOST must not target a remote execution daemon")
        endpoint = self.command(
            ["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], 15
        ).stdout.strip()
        if not endpoint.startswith("unix://"):
            raise SandboxError("A local Docker Desktop Unix socket is required; remote daemons are forbidden")
        self.command(["info", "--format", "{{.OSType}}"], 15)

    def command(self, args: list[str], timeout: float = 30, check: bool = True) -> Outcome:
        outcome = run(["docker", *args], timeout, self.cancel)
        if check and (outcome.exit_code or outcome.timed_out or outcome.cancelled):
            raise SandboxError(
                f"Docker {args[0]} failed (exit={outcome.exit_code}, timeout={outcome.timed_out})"
            )
        return outcome

    def create(self, job_id: str) -> str:
        self.preflight()
        self.job_id = str(UUID(job_id))
        self.image = f"agentbenchx-job:{self.job_id}"
        return self.job_id

    def prepare(self, sandbox_id: str, agent: Path, problem: Path) -> None:
        self.workspace = problem.parent
        runner = self.workspace / "runner"
        runner.mkdir()
        shutil.copyfile(Path(__file__).with_name("harness.py"), runner / "harness.py")
        shutil.copyfile(agent, runner / "agent.py")
        (runner / "input.json").write_text(
            json.dumps({"instruction": (problem / "instruction.md").read_text(), "job_id": sandbox_id})
        )
        # Only environment/ is the build context. Tests and reference solutions cannot enter its layers.
        build = self.command(
            [
                "build",
                "--platform",
                self.settings.docker_platform,
                "--tag",
                self.image,
                str(problem / "environment"),
            ],
            self.settings.build_timeout_seconds,
            check=False,
        )
        (self.workspace / "build.stdout.log").write_text(build.stdout)
        (self.workspace / "build.stderr.log").write_text(build.stderr)
        if build.exit_code or build.timed_out or build.cancelled:
            raise SandboxError("Benchmark image build failed; inspect the preserved build logs")
        self.image_id = self.command(["image", "inspect", "--format", "{{.Id}}", self.image]).stdout.strip()
        workdir = self.command(
            ["image", "inspect", "--format", "{{.Config.WorkingDir}}", self.image]
        ).stdout.strip()
        if not workdir.startswith("/") or workdir == "/":
            raise SandboxError("Harbor image must declare a repository WORKDIR")

    def execute(self, sandbox_id: str, argv: list[str], timeout_seconds: int) -> Outcome:
        mode = argv[0]
        if mode not in {"agent", "test"} or self.workspace is None:
            raise SandboxError("Invalid sandbox phase")
        if self.cancel.is_set():
            raise SandboxError("Execution cancelled before container creation")
        name = f"abx-{self.job_id}-{mode}"
        self.containers.add(name)  # Track before create: cleanup also covers ambiguous create responses.
        mounts = ["--mount", f"type=bind,src={self.workspace / 'runner'},dst=/runner,readonly"]
        if mode == "test":
            mounts += [
                "--mount",
                f"type=bind,src={self.workspace / 'problem' / 'tests'},dst=/tests,readonly",
                "--mount",
                f"type=bind,src={self.workspace / 'candidate'},dst=/candidate,readonly",
            ]
        self.command(
            [
                "create",
                "--name",
                name,
                "--init",
                "--no-healthcheck",
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "256",
                "--memory",
                f"{self.settings.sandbox_memory_mb}m",
                "--memory-swap",
                f"{self.settings.sandbox_memory_mb}m",
                "--cpus",
                str(self.settings.sandbox_cpus),
                "--ulimit",
                "fsize=67108864:67108864",
                "--log-opt",
                "max-size=10m",
                "--log-opt",
                "max-file=1",
                "--user",
                "agent" if mode == "agent" else "root",
                "--entrypoint",
                "python3",
                *(["--cap-add", "DAC_OVERRIDE"] if mode == "test" else []),
                *mounts,
                self.image_id,
                "/runner/harness.py",
                mode,
            ]
        )
        self.current = name
        outcome = self.command(["start", "--attach", name], timeout_seconds, check=False)
        # Kill the container even when its attach client vanished. Keep stopped container for collection.
        self.stop(name)
        state = run(["docker", "inspect", "--format", "{{json .State}}", name], 10).stdout
        recorded = json.loads(state)
        outcome.exit_code = recorded["ExitCode"]
        return outcome

    def stop(self, name: str) -> None:
        # Do not pass the cancelled lease event to cleanup commands.
        run(["docker", "kill", name], 10)
        checked = run(["docker", "inspect", "--format", "{{.State.Running}}", name], 10)
        if checked.exit_code or checked.stdout.strip() != "false":
            raise CleanupError("Cannot confirm container termination; worker must stop")

    def read_file(self, name: str, filename: str, limit: int) -> bytes:
        """Read one regular file via tar stream, never extract container-controlled paths on the Mac."""
        # Stream to a bounded read. Killing the copy process on overflow closes its pipe.
        process = subprocess.Popen(
            ["docker", "cp", f"{name}:{filename}", "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        timer = threading.Timer(15, process.kill)
        timer.start()
        try:
            data = process.stdout.read(limit + 65537)
            if len(data) > limit + 65536:
                raise SandboxError("Container artifact exceeds size limit")
            process.wait(timeout=2)
            if process.returncode:
                return b""
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                entries = archive.getmembers()
                if len(entries) != 1 or not entries[0].isfile() or entries[0].size > limit:
                    raise SandboxError("Expected one bounded regular artifact file")
                return archive.extractfile(entries[0]).read()
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()

    def collect(self, sandbox_id: str) -> dict:
        if self.current is None:
            return {}
        if self.current.endswith("-agent"):
            return {"patch": self.read_file(self.current, "/tmp/abx-patch.diff", 2_000_000).decode("utf-8")}
        return {
            "junit": self.read_file(self.current, "/logs/verifier/junit.xml", 5_000_000),
            "reward": self.read_file(self.current, "/logs/verifier/reward.txt", 100),
        }

    def destroy(self, sandbox_id: str) -> None:
        failures = []
        for name in self.containers:
            removed = run(["docker", "rm", "--force", "--volumes", name], 15)
            if removed.exit_code and "No such container" not in removed.stderr:
                failures.append(name)
        if failures:
            raise CleanupError("Container cleanup failed; preserve workspace and stop worker")
        if self.image:
            run(["docker", "image", "rm", self.image], 30)
