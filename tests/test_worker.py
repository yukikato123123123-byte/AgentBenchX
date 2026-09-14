"""Trusted worker-unit fixtures only. No uploaded or NIFUS benchmark code executes here."""

import hashlib
import json
import sys
import threading
import time
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx
import pytest
from agentbenchx.schemas import ResultInput
from agentbenchx.services import CostCalculator

from workers.evaluator.client import WorkerClient
from workers.evaluator.config import WorkerSettings
from workers.evaluator.docker_sandbox import CleanupError, DockerSandbox, SandboxError
from workers.evaluator.evaluator import Evaluator
from workers.evaluator.inputs import InputError, verify_agent, workspace
from workers.evaluator.lease import LeaseKeeper
from workers.evaluator.main import identity, replay_pending
from workers.evaluator.process import Outcome, run
from workers.evaluator.results import parse_junit, patch_metrics


@pytest.fixture
def settings(tmp_path):
    return WorkerSettings(
        _env_file=None,
        server_url="http://localhost:8000",
        workspace_root=tmp_path,
    )


def outcome(exit_code=0, timed_out=False):
    return Outcome(
        exit_code,
        "trusted fixture stdout",
        "trusted fixture stderr",
        "2026-09-14T00:00:00Z",
        "2026-09-14T00:00:01Z",
        1,
        timed_out=timed_out,
    )


def job():
    content = b"# Never executed by these tests\n"
    return {
        "id": str(uuid4()),
        "lease_token": "lease",
        "lease_seconds": 120,
        "agent_version": {
            "filename": "agent.py",
            "version": 1,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
        "problem_version": {
            "id": str(uuid4()),
            "timeout_seconds": 3,
            "test_command": ["bash", "/tests/test.sh"],
        },
    }, content


class FixtureSandbox:
    image_id = "sha256:fixture"
    mode = "success"
    instances = []

    def __init__(self, settings, cancel):
        self.destroyed = False
        self.phase = None
        self.commands = []
        self.instances.append(self)

    def create(self, identifier):
        return identifier

    def prepare(self, identifier, agent, problem):
        pass

    def execute(self, identifier, argv, timeout):
        self.phase = argv[0]
        self.commands.append((argv, timeout))
        if self.mode == "timeout" and self.phase == "agent":
            return outcome(-9, True)
        return outcome(1 if self.mode == "failure" and self.phase == "test" else 0)

    def collect(self, identifier):
        if self.phase == "agent":
            return {"patch": "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"}
        error = '<failure message="controlled test failure"/>' if self.mode == "failure" else ""
        return {
            "junit": f'<testsuite><testcase name="trusted_fixture" time="1">{error}</testcase></testsuite>'.encode(),
            "reward": b"0" if error else b"1",
        }

    def destroy(self, identifier):
        self.destroyed = True


def run_fixture(settings, mode):
    assignment, content = job()
    client = Mock()
    client.download_agent.return_value = content
    lease = Mock(cancel=threading.Event())
    FixtureSandbox.mode = mode
    with patch(
        "workers.evaluator.evaluator.prepare_problem",
        side_effect=lambda version, root, hosts: root / "problem",
    ):
        result, folder = Evaluator(settings, client, FixtureSandbox).evaluate(assignment, str(uuid4()), lease)
    return result, folder, lease


@pytest.mark.parametrize(
    "mode,status,kind",
    [
        ("success", "COMPLETED", None),
        ("failure", "FAILED", "TEST_FAILURE"),
        ("timeout", "TIMEOUT", "TIMEOUT"),
    ],
)
def test_evaluation_outcomes_and_artifacts(settings, mode, status, kind):
    result, folder, lease = run_fixture(settings, mode)
    validated = ResultInput(**result)
    assert validated.status == status and validated.failure_type == kind
    assert result["metrics"]["input_tokens"] is None and result["metrics"]["llm_cost"] is None
    assert result["execution"]["agent"]["exit_code"] == (-9 if mode == "timeout" else 0)
    assert FixtureSandbox.instances[-1].destroyed
    assert (folder.parent / "result.pending.json").exists()
    assert (folder.parent / "result.pending.json").stat().st_mode & 0o077 == 0
    if mode == "success":
        assert [c.args[0] for c in lease.renew.call_args_list] == [
            "agent.started",
            "agent.finished",
            "test.started",
            "test.finished",
        ]
        assert FixtureSandbox.instances[-1].commands == [(["agent"], 3), (["test"], 3)]
        Evaluator.acknowledged(folder, {"id": "receipt", "status": status})
        assert not folder.exists() and (folder.parent / "receipt.json").exists()


def test_worker_can_evaluate_after_failure_and_timeout(settings):
    folders = []
    for mode in ["failure", "timeout", "success"]:
        result, folder, _ = run_fixture(settings, mode)
        folders.append(folder)
    assert result["status"] == "COMPLETED"
    assert len(set(folders)) == 3


def test_cleanup_failure_is_fatal(settings):
    with patch.object(FixtureSandbox, "destroy", side_effect=CleanupError("fixture")):
        with pytest.raises(CleanupError):
            run_fixture(settings, "success")
    assert list(settings.workspace_root.glob("evaluations/*/result.pending.json"))


def test_checksum_and_version_validation():
    assignment, content = job()
    verify_agent(assignment["agent_version"], content)
    for field, value in [("sha256", "0" * 64), ("filename", "../agent.py"), ("size", 999), ("version", 0)]:
        with pytest.raises(InputError):
            verify_agent({**assignment["agent_version"], field: value}, content)


def test_workspace_escape_and_reuse(tmp_path):
    with pytest.raises(ValueError):
        workspace(tmp_path, "../../escape")
    identifier = str(uuid4())
    workspace(tmp_path, identifier)
    with pytest.raises(FileExistsError):
        workspace(tmp_path, identifier)
    other = tmp_path / "elsewhere"
    other.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "evaluations").symlink_to(other, target_is_directory=True)
    with pytest.raises(InputError):
        workspace(root, str(uuid4()))


def test_real_timeout_and_recovery_for_trusted_process():
    result = run(
        [sys.executable, "-c", "import time; print('trusted timer fixture', flush=True); time.sleep(30)"],
        0.15,
    )
    assert result.timed_out and result.runtime_seconds < 3
    assert "trusted timer fixture" in result.stdout
    assert run([sys.executable, "-c", "print('next trusted process')"], 2).exit_code == 0


def test_output_is_bounded_and_drained():
    result = run(
        [sys.executable, "-c", "import sys; print('x'*100000); print('y'*100000,file=sys.stderr)"],
        2,
        limit=100,
    )
    assert len(result.stdout) < 150 and len(result.stderr) < 150
    assert "truncated" in result.stdout


def test_lease_renews_and_cancels_on_failure():
    client = Mock()
    assignment, _ = job()
    assignment["lease_seconds"] = 0.4
    with LeaseKeeper(client, "worker", assignment, interval=0.02) as lease:
        time.sleep(0.06)
        assert client.job_heartbeat.call_count >= 2
        client.job_heartbeat.side_effect = httpx.ConnectError("unavailable")
        assert lease.cancel.wait(0.5)


def test_transient_lease_failure_retries_without_extending_deadline():
    assignment, _ = job()
    client = Mock()
    lease = LeaseKeeper(client, "worker", assignment)
    previous = lease.deadline
    client.job_heartbeat.side_effect = [httpx.ConnectError("fixture"), None]
    with patch.object(lease.cancel, "wait", return_value=False) as wait:
        lease.renew()
    assert client.job_heartbeat.call_count == 2
    wait.assert_called_once()
    assert not lease.cancel.is_set() and lease.deadline >= previous


def test_lease_does_not_retry_rejected_ownership():
    assignment, _ = job()
    response = httpx.Response(403, request=httpx.Request("POST", "https://fixture.invalid"))
    client = Mock()
    client.job_heartbeat.side_effect = httpx.HTTPStatusError(
        "fixture", request=response.request, response=response
    )
    lease = LeaseKeeper(client, "worker", assignment)
    with pytest.raises(httpx.HTTPStatusError):
        lease.renew()
    assert lease.cancel.is_set() and client.job_heartbeat.call_count == 1


def test_watchdog_cancels_while_network_request_is_blocked():
    client = Mock()
    assignment, _ = job()
    assignment["lease_seconds"] = 0.2
    calls = 0

    def heartbeat(*args):
        nonlocal calls
        calls += 1
        if calls > 1:
            time.sleep(0.4)

    client.job_heartbeat.side_effect = heartbeat
    with LeaseKeeper(client, "worker", assignment, interval=0.01) as lease:
        assert lease.cancel.wait(0.35)


def test_mac_guard_before_any_docker_command(settings):
    with (
        patch("workers.evaluator.docker_sandbox.platform.system", return_value="Linux"),
        patch("workers.evaluator.docker_sandbox.run") as command,
    ):
        with pytest.raises(SandboxError, match="not Darwin"):
            DockerSandbox(settings, threading.Event()).preflight()
        command.assert_not_called()


def test_docker_mount_separation(settings, tmp_path):
    sandbox = DockerSandbox(settings, threading.Event())
    sandbox.workspace = tmp_path
    sandbox.job_id = str(uuid4())
    sandbox.image_id = "sha256:image"
    with (
        patch.object(sandbox, "command", return_value=outcome()) as command,
        patch.object(sandbox, "stop"),
        patch(
            "workers.evaluator.docker_sandbox.run", return_value=Outcome(0, '{"ExitCode":0}', "", "", "", 0)
        ),
    ):
        sandbox.execute(sandbox.job_id, ["agent"], 3)
        args = command.call_args_list[0].args[0]
        assert "none" == args[args.index("--network") + 1]
        assert "agent" == args[args.index("--user") + 1]
        assert not any(
            "/tests" in a or ".ssh" in a or "docker.sock" in a or "WORKER_TOKEN" in a for a in args
        )
        assert "--privileged" not in args and "--env-file" not in args
        sandbox.execute(sandbox.job_id, ["test"], 3)
        args = command.call_args_list[2].args[0]
        assert any("dst=/tests,readonly" in a for a in args)
        assert any("dst=/candidate,readonly" in a for a in args)


def test_junit_counts_and_unknown_metrics():
    rows = parse_junit(
        b'<testsuite><testcase name="p"/><testcase name="f"><failure/></testcase><testcase name="s"><skipped/></testcase></testsuite>'
    )
    assert [r["status"] for r in rows] == ["PASSED", "FAILED", "SKIPPED"]
    result = ResultInput(
        lease_token="x",
        status="FAILED",
        failure_type="TEST_FAILURE",
        metrics={"runtime_seconds": 1},
        tests=rows,
    )
    metrics = CostCalculator.calculate(result)
    assert metrics["total_tokens"] is None and metrics["total_cost"] is None
    assert metrics["score"] == 0.5 and metrics["tests_skipped"] == 1
    with pytest.raises(ValueError):
        parse_junit(b"<!DOCTYPE x><testsuite/>")
    with pytest.raises(ValueError):
        parse_junit(b"<testsuite/>")


def test_patch_summary():
    assert patch_metrics("diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1,2 @@\n-old\n+new\n+line\n") == {
        "files_changed": 1,
        "insertions": 2,
        "deletions": 1,
    }


def test_secure_identity_reuse(settings):
    worker_id = str(uuid4())
    with patch("workers.evaluator.main.WorkerClient") as client:
        client.return_value.register.return_value = {"id": worker_id, "token": "individual-secret"}
        assert identity(settings)[0] == worker_id
        assert identity(settings)[0] == worker_id
        client.return_value.register.assert_called_once()
        assert (settings.workspace_root / "identity.json").stat().st_mode & 0o077 == 0
    (settings.workspace_root / "identity.json").chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        identity(settings)


def test_pending_result_replay(settings):
    path = settings.workspace_root / "evaluations" / str(uuid4())
    path.mkdir(parents=True)
    (path / "result.pending.json").write_text(json.dumps({"lease_token": "lease"}))
    client = Mock()
    client.result.return_value = {"id": "receipt"}
    replay_pending(settings, "worker", client)
    assert (path / "result.accepted.json").exists()
    assert not (path / "result.pending.json").exists()


def test_download_does_not_follow_payload_url():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=b"data")

    client = WorkerClient("https://server.example", "secret")
    client.http = httpx.Client(base_url="https://server.example", transport=httpx.MockTransport(handler))
    assert (
        client.download_agent(
            "worker", {"id": "job", "lease_token": "lease", "agent_download_url": "https://evil.example/"}
        )
        == b"data"
    )
    assert str(requests[0].url) == "https://server.example/api/v1/workers/worker/jobs/job/agent"


def test_source_extraction_uses_exact_commit_and_omits_solutions(tmp_path):
    from workers.evaluator.inputs import prepare_problem

    assignment, _ = job()
    version = {
        "id": str(uuid4()),
        "repository_url": "git@github.com:example/bench.git",
        "commit_sha": "a" * 40,
        "problem_path": "release/task",
    }
    files = {
        f"release/task/{name}"
        for name in [
            "task.toml",
            "instruction.md",
            "environment/Dockerfile",
            "tests/test.sh",
            "solution/fix.patch",
        ]
    }
    with patch("workers.evaluator.inputs.GitRepositoryManager") as manager:
        git = manager.return_value
        git.get_current_commit.return_value = "a" * 40
        git.files.return_value = files
        git.read.return_value = b"fixture metadata, not executable"
        task = prepare_problem(version, tmp_path, ["github.com"])
        git.clone.assert_called_once_with(version["id"], "https://github.com/example/bench.git")
        git.get_current_commit.assert_called_once_with(git.clone.return_value, "a" * 40)
        assert (task / "environment/Dockerfile").is_file()
        assert not (task / "solution").exists()
        assert not any("solution" in call.args[-1] for call in git.read.call_args_list)


def test_source_rejects_commit_mismatch(tmp_path):
    from workers.evaluator.inputs import prepare_problem

    version = {
        "id": str(uuid4()),
        "repository_url": "git@github.com:example/bench.git",
        "commit_sha": "a" * 40,
        "problem_path": "release/task",
    }
    with patch("workers.evaluator.inputs.GitRepositoryManager") as manager:
        manager.return_value.get_current_commit.return_value = "b" * 40
        with pytest.raises(InputError, match="commit mismatch"):
            prepare_problem(version, tmp_path, ["github.com"])


def test_crash_recovery_removes_only_recorded_job_containers(settings):
    from workers.evaluator.main import recover_sandboxes

    identifier = str(uuid4())
    (settings.workspace_root / "evaluations" / identifier / "workspace").mkdir(parents=True)
    with patch("workers.evaluator.main.DockerSandbox") as sandbox:
        recover_sandboxes(settings)
        assert sandbox.return_value.containers == {f"abx-{identifier}-agent", f"abx-{identifier}-test"}
        sandbox.return_value.destroy.assert_called_once_with(identifier)


def test_remote_docker_environment_cannot_bypass_mac_guard(settings, monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://control-plane:2375")
    with (
        patch("workers.evaluator.docker_sandbox.platform.system", return_value="Darwin"),
        patch("workers.evaluator.docker_sandbox.run") as command,
    ):
        with pytest.raises(SandboxError, match="remote"):
            DockerSandbox(settings, threading.Event()).preflight()
        command.assert_not_called()
