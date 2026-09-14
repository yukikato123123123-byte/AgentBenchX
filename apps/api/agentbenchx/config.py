from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    database_url: str = "postgresql+psycopg://agentbenchx:local-development-only@localhost:5432/agentbenchx"
    redis_url: str = "redis://localhost:6379/0"
    storage_root: Path = Path("storage")
    admin_token: str
    web_origin: str = "http://localhost:3000"
    git_allowed_hosts: list[str] = ["github.com"]
    lease_seconds: int = Field(default=120, ge=30)
    worker_offline_seconds: int = Field(default=120, ge=120)
    reaper_interval_seconds: float = Field(default=30, ge=1, le=120)
    db_pool_size: int = Field(default=4, ge=1, le=20)
    db_max_overflow: int = Field(default=1, ge=0, le=10)
    db_pool_timeout: float = Field(default=5, gt=0)
    storage_backend: Literal["local", "s3"] = "local"
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str = Field(default="auto", min_length=1)
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None

    @model_validator(mode="after")
    def storage_valid(self):
        if self.storage_backend == "s3":
            if not all((self.s3_bucket, self.s3_access_key_id, self.s3_secret_access_key)):
                raise ValueError("S3 storage requires bucket and server-side credentials")
            if self.s3_endpoint_url:
                endpoint = urlsplit(self.s3_endpoint_url)
                if (
                    endpoint.scheme != "https"
                    or not endpoint.hostname
                    or endpoint.username
                    or endpoint.password
                    or endpoint.query
                    or endpoint.fragment
                    or endpoint.path not in {"", "/"}
                ):
                    raise ValueError("S3 endpoint must be an HTTPS origin without credentials or paths")
        return self

    max_attempts: int = 3
    upload_limit: int = 2_000_000
