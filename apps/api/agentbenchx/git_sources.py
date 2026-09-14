"""Git is used only as a data reader; no task imports, hooks, builds or tests."""

import os
import re
import subprocess
import tomllib
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select

from .models import Problem, ProblemSource, ProblemVersion, now


class SourceError(ValueError):
    pass


class GitRepositoryManager:
    def __init__(self, root: Path, allowed_hosts: list[str]):
        self.root, self.allowed_hosts = root, allowed_hosts

    def validate_url(self, url: str) -> str:
        ssh = re.fullmatch(r"git@([A-Za-z0-9.-]+):([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git)", url)
        if ssh:
            host = ssh.group(1)
        else:
            try:
                parsed = urlsplit(url)
                port = parsed.port
            except ValueError as exc:
                raise SourceError("Invalid repository URL") from exc
            if (
                parsed.scheme != "https"
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or port not in (None, 443)
                or not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", parsed.path)
            ):
                raise SourceError("Use an approved Git SSH or HTTPS repository URL")
            host = parsed.hostname
        if host not in self.allowed_hosts:
            raise SourceError("Repository host is not allowed")
        return url

    def path(self, source_id: str) -> Path:
        return self.root / "problem_sources" / str(UUID(source_id)) / "repository"

    def run(self, *args: str, cwd: Path | None = None, binary: bool = False):
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        env.pop("GIT_ASKPASS", None)
        env.pop("SSH_ASKPASS", None)
        env.pop("GIT_CREDENTIAL_HELPER", None)
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes")
        try:
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "protocol.file.allow=never",
                    "-c",
                    "protocol.ext.allow=never",
                    *args,
                ],
                cwd=cwd,
                env=env,
                capture_output=True,
                timeout=120,
                check=True,
            )
            return result.stdout if binary else result.stdout.decode().strip()
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode(errors="replace").strip()
            raise SourceError(
                f"Git operation failed: {stderr[:1000]}"
            ) from exc
        except (subprocess.SubprocessError, OSError) as exc:
            raise SourceError(f"Git operation failed: {exc}") from exc


    def clone(self, source_id: str, url: str) -> Path:
        import logging; logging.warning("DEBUG CLONE URL=%r", url)
        path = self.path(source_id)
        self.validate_url(url)
        path.parent.mkdir(parents=True, exist_ok=True)

        debug = self.run("ls-remote", url)
        import logging
        logging.warning("DEBUG LS-REMOTE=%s", debug[:200])

        self.run(
            "-c",
            "credential.helper=",
            "clone",
            "--bare",
            url,
            str(path),
        )
        return path

    def fetch(self, path: Path):
        self.run("fetch", "--prune", "origin", "+refs/heads/*:refs/heads/*", cwd=path)

    def get_branch(self, path: Path) -> str:
        return self.run("symbolic-ref", "--short", "HEAD", cwd=path)

    def checkout(self, path: Path, branch: str) -> str:
        """Resolve a branch without checking untrusted files into the control plane."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", branch) or ".." in branch:
            raise SourceError("Invalid branch")
        return self.get_current_commit(path, f"refs/heads/{branch}")

    def get_current_commit(self, path: Path, ref: str = "HEAD") -> str:
        sha = self.run("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=path)
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise SourceError("Invalid commit SHA")
        return sha

    def pin(self, path: Path, sha: str) -> None:
        """Keep historical commits reachable even after upstream force-pushes."""
        self.run("update-ref", f"refs/agentbenchx/{sha}", sha, cwd=path)

    def files(self, path: Path, sha: str) -> set[str]:
        entries = self.run("ls-tree", "-rz", sha, cwd=path, binary=True).decode().split("\0")
        return {e.split("\t", 1)[1] for e in entries if e.startswith(("100644 blob", "100755 blob"))}

    def read(self, path: Path, sha: str, filename: str) -> bytes:
        size = int(self.run("cat-file", "-s", f"{sha}:{filename}", cwd=path))
        if size > 1_000_000:
            raise SourceError("Metadata exceeds 1 MB")
        return self.run("show", f"{sha}:{filename}", cwd=path, binary=True)


class ProblemValidationService:
    def validate(self, task: dict) -> dict:
        path = PurePosixPath(task["path"])
        if path.is_absolute() or ".." in path.parts:
            raise SourceError("Unsafe problem path")
        if task["category"] not in {"CODING", "DATABASE", "DATA", "API_BACKEND"}:
            raise SourceError("Unsupported category")
        if not 1 <= task["timeout"] <= 86400:
            raise SourceError("Timeout must be between 1 and 86400 seconds")
        if task["difficulty"] not in {"EASY", "MEDIUM", "HARD", "EXPERT"}:
            raise SourceError("Unsupported difficulty")
        return task


class ProblemDiscoveryService:
    """Harbor adapter based on inspected NIFUS task.toml files, not assumed YAML."""

    def discover(self, git: GitRepositoryManager, path: Path, sha: str) -> tuple[list[dict], list[dict]]:
        files = git.files(path, sha)
        tasks, skipped = [], []
        for filename in sorted(f for f in files if f.endswith("/task.toml")):
            folder = str(PurePosixPath(filename).parent)
            try:
                raw = tomllib.loads(git.read(path, sha, filename).decode())
                meta = raw.get("metadata", {})
                if not isinstance(meta, dict):
                    raise SourceError("Metadata must be a table")
                tags = meta.get("tags", [])
                category = meta.get("category", "")
                if (
                    not isinstance(category, str)
                    or not isinstance(tags, list)
                    or not all(isinstance(tag, str) for tag in tags)
                ):
                    raise SourceError("Category must be text and tags must be a list of strings")
                # The inspected xarray-spatial family is GIS even when tagged 'bugfix'.
                if (
                    "xarray-spatial" in folder.lower()
                    or category.lower() == "gis"
                    or any(str(t).lower() in {"gis", "geospatial", "geotiff", "rasterization"} for t in tags)
                ):
                    skipped.append({"path": folder, "reason": "GIS is out of scope"})
                    continue
                if not all(
                    f"{folder}/{p}" in files
                    for p in ("instruction.md", "tests/test.sh", "environment/Dockerfile")
                ):
                    raise SourceError("Missing instruction, verifier or environment Dockerfile")
                if category == "database_query_engineering":
                    normalized, kind = (
                        "DATABASE",
                        "QUERY_OPTIMIZATION" if "query-optimization" in tags else "SQL",
                    )
                    if "migration" in tags:
                        kind = "MIGRATION"
                elif category.startswith("linting-"):
                    normalized, kind = "CODING", "REFACTORING"
                elif category == "bugfix":
                    normalized, kind = "CODING", "BUG_FIX"
                elif category in {"CODING", "DATABASE", "DATA", "API_BACKEND"}:
                    normalized, kind = category, meta.get("type", "INTEGRATION")
                else:
                    raise SourceError(f"Unmapped source category: {category}")
                task = {
                    "path": folder,
                    "name": PurePosixPath(folder).name,
                    "category": normalized,
                    "type": kind,
                    "difficulty": meta.get("difficulty", "medium").upper(),
                    "timeout": int(raw.get("verifier", {}).get("timeout_sec", 300)),
                    "command": ["bash", "/tests/test.sh"],
                    "metadata": {
                        "format": "harbor",
                        "source": raw,
                        "tags": tags,
                        "language": "Python" if "python" in tags else "Unknown",
                        "framework": "Django" if "django" in tags else "",
                        "instruction": git.read(path, sha, f"{folder}/instruction.md").decode(),
                    },
                }
                tasks.append(ProblemValidationService().validate(task))
            except (SourceError, ValueError, TypeError, KeyError) as exc:
                skipped.append({"path": folder, "reason": str(exc)})
        return tasks, skipped


class ProblemSourceManager:
    def __init__(self, db, git: GitRepositoryManager):
        self.db, self.git = db, git

    def register(self, data: dict) -> ProblemSource:
        self.git.validate_url(data["repository_url"])
        source = ProblemSource(**data)
        self.db.add(source)
        self.db.commit()
        return source

    def get_status(self, source_id: str) -> ProblemSource | None:
        return self.db.get(ProblemSource, source_id)

    def sync(self, source_id: str) -> ProblemSource:
        source = self.db.scalar(select(ProblemSource).where(ProblemSource.id == source_id).with_for_update())
        if source is None:
            raise SourceError("Source not found")
        if not source.enabled:
            raise SourceError("Source is disabled")
        try:
            path = self.git.path(source.id)
            if path.exists():
                self.git.fetch(path)
            else:
                path = self.git.clone(source.id, source.repository_url)
            branch = source.default_branch or self.git.get_branch(path)
            sha = self.git.checkout(path, branch)
            tasks, skipped = ProblemDiscoveryService().discover(self.git, path, sha)
            self.git.pin(path, sha)
            created = 0
            for task in tasks:
                problem = self.db.scalar(
                    select(Problem).where(Problem.source_id == source.id, Problem.problem_key == task["path"])
                )
                if problem is None:
                    problem = Problem(
                        source_id=source.id,
                        problem_key=task["path"],
                        **{k: task[k] for k in ("name", "category", "type", "difficulty")},
                    )
                    self.db.add(problem)
                    self.db.flush()
                existing = self.db.scalar(
                    select(ProblemVersion).where(
                        ProblemVersion.repository_url == source.repository_url,
                        ProblemVersion.commit_sha == sha,
                        ProblemVersion.problem_path == task["path"],
                    )
                )
                if existing is None:
                    self.db.add(
                        ProblemVersion(
                            problem_id=problem.id,
                            repository_url=source.repository_url,
                            branch=branch,
                            commit_sha=sha,
                            problem_path=task["path"],
                            metadata_json=task["metadata"],
                            test_command=task["command"],
                            timeout_seconds=task["timeout"],
                        )
                    )
                    created += 1
            source.default_branch = branch
            source.last_commit_sha = sha
            source.last_synced_at = now()
            source.sync_status = "READY" if tasks else "EMPTY"
            source.sync_report = {"discovered": len(tasks), "versions_created": created, "skipped": skipped}
            self.db.commit()
            return source
        except Exception:
            self.db.rollback()
            source = self.db.get(ProblemSource, source_id)
            source.sync_status = "FAILED"
            source.sync_report = {"error": "Sync failed; verify Git access and repository metadata"}
            self.db.commit()
            raise
