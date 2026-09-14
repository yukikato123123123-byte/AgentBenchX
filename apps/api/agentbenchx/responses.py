"""Typed public records. Private credentials and storage paths are intentionally excluded."""

from datetime import datetime

from pydantic import BaseModel


class AgentResponse(BaseModel):
    name: str
    description: str
    id: str
    created_at: datetime


class AgentVersionResponse(BaseModel):
    agent_id: str
    filename: str
    sha256: str
    size: int
    version: int
    id: str
    created_at: datetime


class ProblemSourceResponse(BaseModel):
    name: str
    repository_url: str
    default_branch: str | None
    enabled: bool
    sync_status: str
    last_synced_at: datetime | None
    last_commit_sha: str | None
    updated_at: datetime
    sync_report: dict
    id: str
    created_at: datetime


class ProblemResponse(BaseModel):
    source_id: str
    problem_key: str
    name: str
    category: str
    type: str
    difficulty: str
    updated_at: datetime
    id: str
    created_at: datetime

    latest_version: "ProblemVersionResponse | None" = None


class ProblemVersionResponse(BaseModel):
    problem_id: str
    repository_url: str
    branch: str
    commit_sha: str
    problem_path: str
    metadata_json: dict
    test_command: list[str]
    timeout_seconds: int
    status: str
    id: str
    created_at: datetime


class SubmissionResponse(BaseModel):
    agent_version_id: str
    problem_version_id: str
    id: str
    created_at: datetime


class WorkerResponse(BaseModel):
    name: str
    hostname: str
    platform: str
    status: str
    version: str
    last_heartbeat: datetime
    capabilities: list[str]
    id: str
    created_at: datetime


class RunnerJobResponse(BaseModel):
    submission_id: str
    worker_id: str | None
    status: str
    attempt: int
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    heartbeat_at: datetime | None
    lease_expires_at: datetime | None
    error_message: str | None
    id: str
    created_at: datetime


class EvaluationResponse(BaseModel):
    job_id: str
    submission_id: str
    worker_id: str
    status: str
    failure_type: str | None
    metrics: dict
    artifact_path: str
    id: str
    created_at: datetime


class TestResultResponse(BaseModel):
    evaluation_id: str
    name: str
    status: str
    duration_seconds: float
    message: str
    id: str
    created_at: datetime


class WorkerCredentialsResponse(WorkerResponse):
    token: str


class JobClaimResponse(RunnerJobResponse):
    lease_token: str
    lease_seconds: int
    problem_version: ProblemVersionResponse
    agent_version: AgentVersionResponse
    agent_download_url: str


class EvaluationDetailResponse(EvaluationResponse):
    submission: SubmissionResponse
    job: RunnerJobResponse
    agent_version: AgentVersionResponse
    problem_version: ProblemVersionResponse
    tests: list[TestResultResponse]
