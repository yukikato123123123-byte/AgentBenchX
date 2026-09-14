from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class AgentCreate(Input):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10000)


class SourceCreate(Input):
    name: str = Field(min_length=1, max_length=200)
    repository_url: str = Field(max_length=500)
    default_branch: str | None = Field(default=None, max_length=200)
    enabled: bool = True


class SubmissionCreate(Input):
    agent_version_id: str
    problem_version_ids: list[str] = Field(min_length=1, max_length=500)


class WorkerRegister(Input):
    name: str = Field(min_length=1, max_length=200)
    hostname: str = Field(min_length=1, max_length=200)
    platform: str = Field(min_length=1, max_length=50)
    version: str = Field(min_length=1, max_length=50)
    capabilities: list[str] = Field(default_factory=list, max_length=30)


class WorkerHeartbeat(Input):
    status: Literal["ONLINE", "DRAINING", "ERROR", "OFFLINE"] = "ONLINE"


class JobHeartbeat(Input):
    lease_token: str
    event: Literal[
        "job.started", "agent.started", "agent.finished", "test.started", "test.progress", "test.finished"
    ] = "job.started"


class TestInput(Input):
    name: str = Field(max_length=500)
    status: Literal["PASSED", "FAILED", "SKIPPED"]
    duration_seconds: float = Field(default=0, ge=0)
    message: str = Field(default="", max_length=10000)


class Metrics(Input):
    runtime_seconds: float = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    llm_cost: float | None = Field(default=None, ge=0)
    compute_cost: float | None = Field(default=None, ge=0)
    files_changed: int = Field(default=0, ge=0)
    insertions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)
    currency: Literal["USD"] = "USD"
    pricing_metadata: dict = Field(default_factory=dict)


class ProcessEvidence(Input):
    exit_code: int
    started_at: str
    finished_at: str
    runtime_seconds: float = Field(ge=0)
    timed_out: bool = False
    cancelled: bool = False


class ExecutionEvidence(Input):
    agent: ProcessEvidence | None = None
    test: ProcessEvidence | None = None
    image_id: str | None = Field(default=None, max_length=200)
    error: str | None = Field(default=None, max_length=10000)


class ResultInput(Input):
    lease_token: str
    status: Literal["COMPLETED", "FAILED", "TIMEOUT"]
    failure_type: (
        Literal["INFRASTRUCTURE_FAILURE", "AGENT_FAILURE", "TEST_FAILURE", "TIMEOUT", "SECURITY_FAILURE"]
        | None
    ) = None
    metrics: Metrics
    execution: ExecutionEvidence | None = None
    tests: list[TestInput] = Field(default_factory=list, max_length=10000)
    stdout: str = Field(default="", max_length=1_000_000)
    stderr: str = Field(default="", max_length=1_000_000)
    agent_output: str = Field(default="", max_length=1_000_000)
    patch: str = Field(default="", max_length=2_000_000)

    @model_validator(mode="after")
    def consistent_status(self):
        if self.status == "COMPLETED" and (
            self.failure_type or any(t.status == "FAILED" for t in self.tests)
        ):
            raise ValueError("Completed results cannot contain failures")
        if self.status != "COMPLETED" and not self.failure_type:
            raise ValueError("Failure type is required")
        if self.status == "TIMEOUT" and self.failure_type != "TIMEOUT":
            raise ValueError("Timeout results must have TIMEOUT failure type")
        return self


class RecordResponse(BaseModel):
    """Explicit JSON record envelope; domain fields are also emitted in OpenAPI schemas export."""

    model_config = ConfigDict(extra="allow")
    id: str


class WorkerCredentials(RecordResponse):
    token: str


class HealthResponse(BaseModel):
    status: str
    database: bool
    redis: bool
