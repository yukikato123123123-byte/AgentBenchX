import hashlib
import re
from pathlib import Path, PurePosixPath
from uuid import UUID

from agentbenchx.git_sources import GitRepositoryManager


class InputError(ValueError):
    pass


def workspace(root: Path, job_id: str) -> Path:
    root = root.resolve()
    path = root / "evaluations" / str(UUID(job_id)) / "workspace"
    if not path.resolve().is_relative_to(root):
        raise InputError("Workspace escapes configured root")
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    return path


def verify_agent(version: dict, content: bytes) -> None:
    if (
        version.get("filename") != "agent.py"
        or not isinstance(version.get("version"), int)
        or version["version"] < 1
        or version.get("size") != len(content)
        or not 0 < len(content) <= 2_000_000
        or hashlib.sha256(content).hexdigest() != version.get("sha256")
    ):
        raise InputError("Agent identity, size or SHA-256 mismatch")


def prepare_problem(version: dict, destination: Path, allowed_hosts: list[str]) -> Path:
    """Read the pinned source over HTTPS; legacy SSH metadata needs no Mac SSH setup."""
    sha = version["commit_sha"]
    path = PurePosixPath(version["problem_path"])
    if (
        not re.fullmatch(r"[0-9a-f]{40,64}", sha)
        or path.is_absolute()
        or ".." in path.parts
        or not path.parts
    ):
        raise InputError("Invalid immutable problem identity")
    git = GitRepositoryManager(destination, allowed_hosts)
    url = version["repository_url"]
    git.validate_url(url)
    ssh = re.fullmatch(r"git@([A-Za-z0-9.-]+):([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git)", url)
    if ssh:
        url = f"https://{ssh[1]}/{ssh[2]}"
    git.validate_url(url)
    cache = git.clone(version["id"], url)
    try:
        actual = git.get_current_commit(cache, sha)
    except ValueError:
        git.run("fetch", "origin", sha, cwd=cache)
        actual = git.get_current_commit(cache, sha)
    if actual != sha:
        raise InputError("Problem commit mismatch")
    task = destination / "problem"
    task.mkdir()
    prefix = str(path) + "/"
    total = 0
    for name in sorted(git.files(cache, sha)):
        if not name.startswith(prefix):
            continue
        relative = PurePosixPath(name[len(prefix) :])
        if ".." in relative.parts or relative.is_absolute():
            raise InputError("Unsafe repository path")
        # Never extract reference answers into task material or expose them to containers.
        if relative.parts[0] == "solution":
            continue
        target = task.joinpath(*relative.parts)
        if not target.resolve().is_relative_to(task.resolve()):
            raise InputError("Unsafe extracted path")
        data = git.read(cache, sha, name)
        total += len(data)
        if total > 50_000_000:
            raise InputError("Task exceeds 50 MB metadata transfer budget")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for required in ("task.toml", "instruction.md", "environment/Dockerfile", "tests/test.sh"):
        if not (task / required).is_file():
            raise InputError(f"Missing required Harbor file: {required}")
    return task
