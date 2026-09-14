import ast
import hashlib
import json
import secrets
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import func, select

from .models import (
    Agent,
    AgentVersion,
    Evaluation,
    EvaluationLog,
    Patch,
    ProblemVersion,
    RunnerJob,
    Score,
    Submission,
    TestResult,
    Worker,
    now,
    uid,
)
from .schemas import ResultInput
from .storage import LocalStorageBackend, storage_backend


def require(db, model, identifier):
    record = db.get(model, identifier)
    if record is None:
        raise HTTPException(404, f"{model.__name__} not found")
    return record


def serialize(record) -> dict:
    return {
        a.key: getattr(record, a.key)
        for a in record.__mapper__.column_attrs
        if a.key not in {"token_hash", "lease_token", "storage_path", "last_event"}
    }


class AgentManager:
    def __init__(self, db, settings, storage=None):
        self.db, self.settings = db, settings
        self.storage = storage or storage_backend(settings)

    def upload(self, agent_id: str, filename: str, content: bytes):
        agent = self.db.scalar(select(Agent).where(Agent.id == agent_id).with_for_update())
        if not agent:
            raise HTTPException(404, "Agent not found")
        if filename != "agent.py" or not content or len(content) > self.settings.upload_limit:
            raise HTTPException(422, "Upload a nonempty agent.py no larger than 2 MB")
        try:
            ast.parse(content.decode("utf-8"))  # Syntax only: never import or execute.
        except (SyntaxError, UnicodeDecodeError, ValueError, RecursionError):
            raise HTTPException(422, "agent.py must be valid UTF-8 Python source")
        digest = hashlib.sha256(content).hexdigest()
        version = (
            self.db.scalar(select(func.max(AgentVersion.version)).where(AgentVersion.agent_id == agent_id))
            or 0
        ) + 1
        identifier = uid()
        relative = Path("agents") / agent_id / identifier / "agent.py"
        self.storage.put(str(relative), content)
        record = AgentVersion(
            id=identifier,
            agent_id=agent_id,
            filename="agent.py",
            sha256=digest,
            size=len(content),
            version=version,
            storage_path=str(relative),
        )
        self.db.add(record)
        self.db.commit()
        return record


class CostCalculator:
    @staticmethod
    def calculate(result: ResultInput) -> dict:
        metrics = result.metrics.model_dump()
        passed = sum(t.status == "PASSED" for t in result.tests)
        failed = sum(t.status == "FAILED" for t in result.tests)
        metrics.update(
            total_tokens=(metrics["input_tokens"] + metrics["output_tokens"])
            if metrics["input_tokens"] is not None and metrics["output_tokens"] is not None
            else None,
            total_cost=round(metrics["llm_cost"] + metrics["compute_cost"], 8)
            if metrics["llm_cost"] is not None and metrics["compute_cost"] is not None
            else None,
            total_tests=len(result.tests),
            tests_passed=passed,
            tests_failed=failed,
            tests_skipped=sum(t.status == "SKIPPED" for t in result.tests),
            score=passed / (passed + failed) if passed + failed else 0,
        )
        metrics["execution"] = result.execution.model_dump() if result.execution else None
        return metrics


class FailureAnalysisService:
    def __init__(self, root):
        self.storage = LocalStorageBackend(root) if isinstance(root, Path) else root

    def generate_basic_report(self, result: ResultInput, context: dict | None = None) -> str:
        values = {
            "Status": result.status,
            **(context or {}),
            "Runtime": result.metrics.runtime_seconds,
            "Failure Type": result.failure_type or "None",
            "Passed Tests": sum(t.status == "PASSED" for t in result.tests),
            "Failed Tests": sum(t.status == "FAILED" for t in result.tests),
            "Skipped Tests": sum(t.status == "SKIPPED" for t in result.tests),
        }
        report = "# Failure Analysis / Execution Evidence\n\n"
        report += "\n\n".join(f"{key}: {value}" for key, value in values.items())
        if result.execution:
            report += "\n\nExecution:\n\n```json\n" + result.execution.model_dump_json(indent=2) + "\n```"
        report += "\n\nError: See stderr.log and agent_output.log.\n\nGenerated Patch: patch.diff\n\n"
        report += "Root Cause: Not inferred. Exit codes, test failures and captured logs are evidence, not a diagnosis.\n"
        return report

    def collect_artifacts(
        self, evaluation_id: str, problem_id: str, result: ResultInput, context: dict | None = None
    ) -> str:
        relative = Path("evaluations") / evaluation_id / problem_id
        artifacts = {
            "stdout.log": result.stdout,
            "stderr.log": result.stderr,
            "agent_output.log": result.agent_output,
            "patch.diff": result.patch,
            "test_result.json": json.dumps([t.model_dump() for t in result.tests], indent=2),
            "analysis.md": self.generate_basic_report(result, context),
        }
        for name, content in artifacts.items():
            self.storage.put(str(relative / name), content.encode("utf-8"))
        return str(relative)


class EvaluationManager:
    """Worker request methods require the Worker row lock acquired by worker_auth.

    Keep that lock through job ownership checks and writes. The reaper intentionally
    releases job locks before locking workers to avoid a lock-order deadlock.
    """
    def __init__(self, db, settings, queue, storage=None):
        self.db, self.settings, self.queue = db, settings, queue
        self.storage = storage or storage_backend(settings)

    def submit(self, data):
        require(self.db, AgentVersion, data.agent_version_id)
        problems = [require(self.db, ProblemVersion, p) for p in dict.fromkeys(data.problem_version_ids)]
        if any(p.status != "READY" for p in problems):
            raise HTTPException(409, "Problem version is not ready")
        submissions, jobs = [], []
        for p in problems:
            submission = Submission(agent_version_id=data.agent_version_id, problem_version_id=p.id)
            self.db.add(submission)
            self.db.flush()
            job = RunnerJob(submission_id=submission.id)
            self.db.add(job)
            submissions.append(submission)
            jobs.append(job)
        self.db.commit()
        for job in jobs:
            self.queue.enqueue(job.id)
            self.queue.publish("job.queued", job_id=job.id)
        return submissions

    def register(self, data):
        token = secrets.token_urlsafe(40)
        worker = Worker(**data.model_dump(), token_hash=hashlib.sha256(token.encode()).hexdigest())
        self.db.add(worker)
        self.db.commit()
        self.queue.publish("worker.status", worker_id=worker.id, status=worker.status)
        return {**serialize(worker), "token": token}

    def worker_heartbeat(self, worker, status):
        active = self.db.scalar(
            select(RunnerJob.id).where(
                RunnerJob.worker_id == worker.id, RunnerJob.status.in_(["CLAIMED", "RUNNING"])
            )
        )
        previous_status = worker.status
        worker.status = "BUSY" if active and status == "ONLINE" else status
        worker.last_heartbeat = now()
        self.db.commit()
        if previous_status != worker.status:
            self.queue.publish("worker.status", worker_id=worker.id, status=worker.status)
        return worker

    def reap(self):
        expired = self.db.scalars(
            select(RunnerJob)
            .where(RunnerJob.status.in_(["CLAIMED", "RUNNING"]), RunnerJob.lease_expires_at < now())
            .with_for_update(skip_locked=True)
        ).all()
        retry_jobs = []
        for job in expired:
            job.status = "FAILED" if job.attempt >= self.settings.max_attempts else "TIMEOUT"
            job.finished_at = now()
            job.error_message = "Worker lease expired; execution outcome unknown"
            if job.attempt < self.settings.max_attempts:
                retry = RunnerJob(submission_id=job.submission_id, attempt=job.attempt + 1)
                self.db.add(retry)
                retry_jobs.append(retry)
        self.db.commit()  # Release job locks before taking worker locks.
        offline = self.db.scalars(
            select(Worker)
            .where(
                Worker.last_heartbeat < now() - timedelta(seconds=self.settings.worker_offline_seconds),
                Worker.status.in_(["ONLINE", "BUSY"]),
            )
            .with_for_update(skip_locked=True)
        ).all()
        for worker in offline:
            worker.status = "OFFLINE"
        self.db.commit()
        for job in expired:
            self.queue.publish("job.failed", job_id=job.id, status=job.status)
        for worker in offline:
            self.queue.publish("worker.status", worker_id=worker.id, status="OFFLINE")
        for retry in retry_jobs:
            self.queue.enqueue(retry.id)
            self.queue.publish("job.queued", job_id=retry.id)

    def claim(self, worker):
        if worker.status in {"DRAINING", "ERROR", "OFFLINE"}:
            raise HTTPException(409, "Worker must be online to claim")
        active = self.db.scalar(
            select(RunnerJob.id).where(
                RunnerJob.worker_id == worker.id, RunnerJob.status.in_(["CLAIMED", "RUNNING"])
            )
        )
        if active:
            raise HTTPException(409, "Worker already has an active job")
        job = self.db.scalar(
            select(RunnerJob)
            .where(RunnerJob.status == "QUEUED")
            .order_by(RunnerJob.queued_at, RunnerJob.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            self.db.commit()
            return None
        job.worker_id, job.status = worker.id, "CLAIMED"
        job.lease_token, job.heartbeat_at = secrets.token_urlsafe(32), now()
        job.lease_expires_at = now() + timedelta(seconds=self.settings.lease_seconds)
        worker.status, worker.last_heartbeat = "BUSY", now()
        submission = require(self.db, Submission, job.submission_id)
        problem = require(self.db, ProblemVersion, submission.problem_version_id)
        agent = require(self.db, AgentVersion, submission.agent_version_id)
        self.db.commit()
        self.queue.claimed(job.id)
        self.queue.publish("worker.status", worker_id=worker.id, status=worker.status)
        return {
            **serialize(job),
            "lease_token": job.lease_token,
            "lease_seconds": self.settings.lease_seconds,
            "problem_version": serialize(problem),
            "agent_version": serialize(agent),
            "agent_download_url": f"/api/v1/workers/{worker.id}/jobs/{job.id}/agent",
        }

    def owned_job(self, worker, job_id, token, allow_terminal=False):
        job = self.db.scalar(select(RunnerJob).where(RunnerJob.id == job_id).with_for_update())
        if not job or job.worker_id != worker.id or not secrets.compare_digest(job.lease_token or "", token):
            raise HTTPException(403, "Invalid job lease")
        if allow_terminal and job.status in {"COMPLETED", "FAILED", "TIMEOUT"}:
            return job
        if job.status not in {"CLAIMED", "RUNNING"} or job.lease_expires_at <= now():
            raise HTTPException(409, "Job lease expired or job is terminal")
        return job

    def heartbeat(self, worker, job_id, data):
        job = self.owned_job(worker, job_id, data.lease_token)
        changed = job.last_event != data.event
        job.last_event = data.event
        job.status, job.heartbeat_at = "RUNNING", now()
        job.started_at = job.started_at or now()
        job.lease_expires_at = now() + timedelta(seconds=self.settings.lease_seconds)
        worker.last_heartbeat = now()
        self.db.commit()
        if changed:
            self.queue.publish(data.event, worker_id=worker.id, job_id=job.id)
        return job

    def result(self, worker, job_id, data):
        job = self.owned_job(worker, job_id, data.lease_token, allow_terminal=True)
        previous = self.db.scalar(select(Evaluation).where(Evaluation.job_id == job.id))
        if previous:
            return previous  # Idempotent retry after a lost HTTP response.
        if job.status not in {"CLAIMED", "RUNNING"} or job.lease_expires_at <= now():
            raise HTTPException(409, "Stale result; lease no longer valid")
        submission = require(self.db, Submission, job.submission_id)
        problem = require(self.db, ProblemVersion, submission.problem_version_id)
        identifier = uid()
        artifacts = FailureAnalysisService(self.storage).collect_artifacts(
            identifier,
            problem.problem_id,
            data,
            {
                "Worker": worker.name,
                "Worker ID": worker.id,
                "Problem": problem.problem_path,
                "Problem ID": problem.problem_id,
                "Problem Version": problem.id,
                "Commit SHA": problem.commit_sha,
                "Attempt": job.attempt,
            },
        )
        # Remote storage can stall while this transaction fences the job. Never
        # accept a result after expiry merely because it was valid before the I/O.
        if job.lease_expires_at <= now():
            raise HTTPException(409, "Result storage exceeded job lease")
        metrics = CostCalculator.calculate(data)
        evaluation = Evaluation(
            id=identifier,
            job_id=job.id,
            submission_id=submission.id,
            worker_id=worker.id,
            status=data.status,
            failure_type=data.failure_type,
            metrics=metrics,
            artifact_path=artifacts,
        )
        self.db.add(evaluation)
        self.db.flush()
        for test in data.tests:
            self.db.add(TestResult(evaluation_id=identifier, **test.model_dump()))
        for kind in ("stdout", "stderr", "agent_output"):
            self.db.add(
                EvaluationLog(evaluation_id=identifier, kind=kind, storage_path=f"{artifacts}/{kind}.log")
            )
        self.db.add(
            Patch(
                evaluation_id=identifier,
                storage_path=f"{artifacts}/patch.diff",
                sha256=hashlib.sha256(data.patch.encode()).hexdigest(),
            )
        )
        self.db.add(Score(evaluation_id=identifier, value=metrics["score"]))
        job.status, job.finished_at = data.status, now()
        job.error_message = data.failure_type
        worker.status = "DRAINING" if worker.status == "DRAINING" else "ONLINE"
        worker.last_heartbeat = now()
        retry = None
        if data.failure_type == "INFRASTRUCTURE_FAILURE" and job.attempt < self.settings.max_attempts:
            retry = RunnerJob(submission_id=submission.id, attempt=job.attempt + 1)
            self.db.add(retry)
        self.db.commit()
        if retry:
            self.queue.enqueue(retry.id)
            self.queue.publish("job.queued", job_id=retry.id)
        self.queue.publish(
            "evaluation.completed" if data.status == "COMPLETED" else "evaluation.failed",
            evaluation_id=identifier,
            job_id=job.id,
        )
        self.queue.publish("worker.status", worker_id=worker.id, status=worker.status)
        return evaluation
