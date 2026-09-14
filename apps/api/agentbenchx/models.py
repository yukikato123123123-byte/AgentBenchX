"""Durable control-plane records. Immutable version tables have no update API."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> datetime:
    return datetime.now(timezone.utc)


def uid() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class User(Record, Base):
    __tablename__ = "users"
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(30), default="ADMIN")


class Agent(Record, Base):
    __tablename__ = "agents"
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")


class AgentVersion(Record, Base):
    __tablename__ = "agent_versions"
    __table_args__ = (UniqueConstraint("agent_id", "version"),)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"))
    filename: Mapped[str] = mapped_column(String(200))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer)
    storage_path: Mapped[str] = mapped_column(Text)


class ProblemSource(Record, Base):
    __tablename__ = "problem_sources"
    name: Mapped[str] = mapped_column(String(200))
    repository_url: Mapped[str] = mapped_column(Text, unique=True)
    default_branch: Mapped[str | None] = mapped_column(String(200), nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    sync_status: Mapped[str] = mapped_column(String(30), default="PENDING")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    sync_report: Mapped[dict] = mapped_column(JSON, default=dict)


class Problem(Record, Base):
    __tablename__ = "problems"
    __table_args__ = (UniqueConstraint("source_id", "problem_key"),)
    source_id: Mapped[str] = mapped_column(ForeignKey("problem_sources.id"))
    problem_key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String(250))
    category: Mapped[str] = mapped_column(String(50))
    type: Mapped[str] = mapped_column(String(50))
    difficulty: Mapped[str] = mapped_column(String(30))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ProblemVersion(Record, Base):
    __tablename__ = "problem_versions"
    __table_args__ = (UniqueConstraint("repository_url", "commit_sha", "problem_path"),)
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.id"))
    repository_url: Mapped[str] = mapped_column(Text)
    branch: Mapped[str] = mapped_column(String(200))
    commit_sha: Mapped[str] = mapped_column(String(64))
    problem_path: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON)
    test_command: Mapped[list] = mapped_column(JSON)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="READY")


class Submission(Record, Base):
    __tablename__ = "submissions"
    agent_version_id: Mapped[str] = mapped_column(ForeignKey("agent_versions.id"))
    problem_version_id: Mapped[str] = mapped_column(ForeignKey("problem_versions.id"))


class Worker(Record, Base):
    __tablename__ = "workers"
    name: Mapped[str] = mapped_column(String(200))
    hostname: Mapped[str] = mapped_column(String(200))
    platform: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(30), default="ONLINE")
    version: Mapped[str] = mapped_column(String(50))
    last_heartbeat: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    token_hash: Mapped[str] = mapped_column(String(64))


class RunnerJob(Record, Base):
    __tablename__ = "runner_jobs"
    __table_args__ = (UniqueConstraint("submission_id", "attempt"),)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id"), index=True)
    worker_id: Mapped[str | None] = mapped_column(ForeignKey("workers.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_event: Mapped[str | None] = mapped_column(String(50), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class Evaluation(Record, Base):
    __tablename__ = "evaluations"
    job_id: Mapped[str] = mapped_column(ForeignKey("runner_jobs.id"), unique=True)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id"))
    worker_id: Mapped[str] = mapped_column(ForeignKey("workers.id"))
    status: Mapped[str] = mapped_column(String(30))
    failure_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    metrics: Mapped[dict] = mapped_column(JSON)
    artifact_path: Mapped[str] = mapped_column(Text)


class TestResult(Record, Base):
    __tablename__ = "test_results"
    evaluation_id: Mapped[str] = mapped_column(ForeignKey("evaluations.id"), index=True)
    name: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(30))
    duration_seconds: Mapped[float] = mapped_column(default=0)
    message: Mapped[str] = mapped_column(Text, default="")


class EvaluationLog(Record, Base):
    __tablename__ = "evaluation_logs"
    evaluation_id: Mapped[str] = mapped_column(ForeignKey("evaluations.id"))
    kind: Mapped[str] = mapped_column(String(50))
    storage_path: Mapped[str] = mapped_column(Text)


class Patch(Record, Base):
    __tablename__ = "patches"
    evaluation_id: Mapped[str] = mapped_column(ForeignKey("evaluations.id"), unique=True)
    storage_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))


class Score(Record, Base):
    __tablename__ = "scores"
    evaluation_id: Mapped[str] = mapped_column(ForeignKey("evaluations.id"), unique=True)
    value: Mapped[float] = mapped_column(Numeric(8, 4))
    method: Mapped[str] = mapped_column(String(100), default="passed / (passed + failed)")


@event.listens_for(AgentVersion, "before_update")
@event.listens_for(ProblemVersion, "before_update")
def immutable_version(mapper, connection, target):
    raise ValueError("Version records are immutable; create a new version")
