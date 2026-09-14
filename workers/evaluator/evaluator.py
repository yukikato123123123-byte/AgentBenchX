import json
import shutil
import time
from dataclasses import asdict
from pathlib import Path

from .docker_sandbox import CleanupError, DockerSandbox
from .inputs import InputError, prepare_problem, verify_agent, workspace
from .results import parse_junit, patch_metrics


class Evaluator:
    def __init__(self, settings, client, sandbox_factory=DockerSandbox):
        self.settings, self.client, self.sandbox_factory = settings, client, sandbox_factory

    def evaluate(self, job: dict, worker_id: str, lease) -> tuple[dict, Path]:
        started = time.monotonic()
        folder = workspace(self.settings.workspace_root, job["id"])
        sandbox = self.sandbox_factory(self.settings, lease.cancel)
        result = {
            "lease_token": job["lease_token"],
            "status": "FAILED",
            "failure_type": "INFRASTRUCTURE_FAILURE",
            "metrics": {
                "runtime_seconds": 0,
                "input_tokens": None,
                "output_tokens": None,
                "llm_cost": None,
                "compute_cost": None,
            },
            "tests": [],
            "stdout": "",
            "stderr": "",
            "agent_output": "",
            "patch": "",
            "execution": {"agent": None, "test": None, "image_id": None, "error": None},
        }
        sandbox_id = job["id"]
        try:
            sandbox.create(sandbox_id)
            content = self.client.download_agent(worker_id, job)
            verify_agent(job["agent_version"], content)
            agent = folder / "agent.py"
            agent.write_bytes(content)
            problem = prepare_problem(job["problem_version"], folder, self.settings.git_allowed_hosts)
            if lease.cancel.is_set():
                raise RuntimeError("Lease renewal failed during input preparation")
            sandbox.prepare(sandbox_id, agent, problem)
            result["execution"]["image_id"] = sandbox.image_id
            timeout = job["problem_version"]["timeout_seconds"]
            lease.renew("agent.started")
            outcome = sandbox.execute(sandbox_id, ["agent"], timeout)
            result["execution"]["agent"] = {
                k: v for k, v in asdict(outcome).items() if k not in {"stdout", "stderr"}
            }
            result["agent_output"], result["stderr"] = outcome.stdout, outcome.stderr
            result["patch"] = sandbox.collect(sandbox_id).get("patch", "")
            if outcome.timed_out or outcome.cancelled:
                result.update(status="TIMEOUT", failure_type="TIMEOUT")
            elif outcome.exit_code:
                result["failure_type"] = "AGENT_FAILURE"
            else:
                lease.renew("agent.finished")
                candidate = folder / "candidate"
                candidate.mkdir()
                (candidate / "patch.diff").write_text(result["patch"])
                (candidate / "test-command.json").write_text(
                    json.dumps(job["problem_version"]["test_command"])
                )
                lease.renew("test.started")
                test = sandbox.execute(sandbox_id, ["test"], timeout)
                result["execution"]["test"] = {
                    k: v for k, v in asdict(test).items() if k not in {"stdout", "stderr"}
                }
                result["stdout"] = test.stdout
                result["stderr"] = (result["stderr"] + "\n" + test.stderr)[-1_000_000:]
                collected = sandbox.collect(sandbox_id)
                try:
                    result["tests"] = parse_junit(collected.get("junit", b""))
                except ValueError as exc:
                    result["execution"]["error"] = str(exc)
                if test.timed_out or test.cancelled:
                    result.update(status="TIMEOUT", failure_type="TIMEOUT")
                elif not result["tests"]:
                    result["failure_type"] = "INFRASTRUCTURE_FAILURE"
                elif (
                    test.exit_code
                    or any(t["status"] == "FAILED" for t in result["tests"])
                    or collected.get("reward", b"").strip() == b"0"
                ):
                    result["failure_type"] = "TEST_FAILURE"
                else:
                    result.update(status="COMPLETED", failure_type=None)
                if not lease.cancel.is_set():
                    lease.renew("test.finished")
        except CleanupError:
            raise
        except InputError as exc:
            result["failure_type"] = "SECURITY_FAILURE"
            result["execution"]["error"] = str(exc)
        except Exception as exc:
            # Do not stringify arbitrary transport errors: they may embed request credentials.
            result["execution"]["error"] = f"{type(exc).__name__}: worker preparation or execution failed"
            for name in ("build.stdout.log", "build.stderr.log"):
                if (folder / name).exists():
                    field = "stdout" if "stdout" in name else "stderr"
                    result[field] = (folder / name).read_text()[-500_000:]
        finally:
            result["metrics"].update(
                runtime_seconds=time.monotonic() - started, **patch_metrics(result["patch"])
            )
            # Local evidence survives network loss and cleanup failures. No worker token is in this file.
            (folder.parent / "result.pending.json").write_text(json.dumps(result, indent=2))
            (folder.parent / "result.pending.json").chmod(0o600)
            sandbox.destroy(sandbox_id)
        return result, folder

    @staticmethod
    def acknowledged(folder: Path, evaluation: dict):
        (folder.parent / "receipt.json").write_text(json.dumps(evaluation, indent=2))
        pending = folder.parent / "result.pending.json"
        pending.rename(folder.parent / "result.accepted.json")
        shutil.rmtree(folder)  # Own UUID workspace only, after all containers are removed and receipt saved.
