"""Shared sandbox interface; DockerSandbox is the Mac implementation. No host fallback."""

from abc import ABC, abstractmethod
from pathlib import Path

from .process import Outcome


class SandboxManager(ABC):
    @abstractmethod
    def create(self, job_id: str) -> str: ...

    @abstractmethod
    def prepare(self, sandbox_id: str, agent: Path, problem: Path) -> None: ...

    @abstractmethod
    def execute(self, sandbox_id: str, argv: list[str], timeout_seconds: int) -> Outcome: ...

    @abstractmethod
    def collect(self, sandbox_id: str) -> dict: ...

    @abstractmethod
    def destroy(self, sandbox_id: str) -> None: ...
