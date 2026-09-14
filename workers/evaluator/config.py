"""Mac-local configuration; never reads the server's .env implicitly."""

from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env.worker", extra="ignore", hide_input_in_errors=True)
    server_url: str
    worker_id: str | None = None
    worker_name: str = "Mac #1"
    worker_token: SecretStr | None = None
    worker_poll_interval: float = Field(default=5, ge=1)
    worker_heartbeat_interval: float = Field(default=20, ge=1, le=30)
    worker_idle_poll_min_seconds: float | None = Field(default=None, ge=1)
    worker_idle_poll_max_seconds: float | None = Field(default=None, ge=1)
    worker_idle_heartbeat_seconds: float = Field(default=45, ge=1, le=60)
    workspace_root: Path = Path("~/Library/Application Support/AgentBenchX").expanduser()
    git_allowed_hosts: list[str] = ["github.com"]
    docker_platform: str = "linux/amd64"
    sandbox_cpus: float = Field(default=2, gt=0, le=8)
    sandbox_memory_mb: int = Field(default=3072, ge=256, le=16384)
    build_timeout_seconds: int = Field(default=1800, ge=30, le=7200)

    @model_validator(mode="after")
    def valid(self):
        if self.worker_idle_poll_min_seconds is None:
            self.worker_idle_poll_min_seconds = self.worker_poll_interval
        if self.worker_idle_poll_max_seconds is None:
            self.worker_idle_poll_max_seconds = max(30, self.worker_idle_poll_min_seconds)
        if self.worker_idle_poll_max_seconds < self.worker_idle_poll_min_seconds:
            raise ValueError("Idle poll maximum must be at least the minimum")
        url = urlsplit(self.server_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("SERVER_URL must be an HTTP(S) origin without credentials")
        if bool(self.worker_id) != bool(self.worker_token):
            raise ValueError("Set WORKER_ID and WORKER_TOKEN together, or use saved credentials")
        self.workspace_root = self.workspace_root.expanduser().resolve()
        if any(c in str(self.workspace_root) for c in (",", "\n", "\r")):
            raise ValueError("WORKSPACE_ROOT contains unsupported mount-path characters")
        return self
