import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from agentbenchx.git_sources import GitRepositoryManager, ProblemDiscoveryService, SourceError


class FakeGit:
    def files(self, *args):
        return {
            f"{folder}/{file}"
            for folder in ("release/code", "db/task", "release/xarray-spatial-gis")
            for file in ("task.toml", "instruction.md", "tests/test.sh", "environment/Dockerfile")
        }

    def read(self, path, sha, filename):
        if filename.endswith("instruction.md"):
            return b"Repair the function"
        category = "database_query_engineering" if filename.startswith("db/") else "bugfix"
        return f'[metadata]\ncategory="{category}"\ndifficulty="medium"\ntags=["python"]\n[verifier]\ntimeout_sec=600'.encode()


def test_harbor_discovery_and_gis_exclusion():
    tasks, skipped = ProblemDiscoveryService().discover(FakeGit(), Path("unused"), "a" * 40)
    assert len(tasks) == 2
    assert {t["category"] for t in tasks} == {"CODING", "DATABASE"}
    assert tasks[0]["command"] == ["bash", "/tests/test.sh"]
    assert len(skipped) == 1 and "GIS" in skipped[0]["reason"]


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/x",
        "--upload-pack=bad",
        "git@evil.org:a/b.git",
        "https://token@github.com/a/b",
        "https://github.com/a/b?token=x",
        "https://github.com:80/a/b",
        "https://github.com:notaport/a/b",
    ],
)
def test_reject_unsafe_urls(tmp_path, url):
    with pytest.raises(SourceError):
        GitRepositoryManager(tmp_path, ["github.com"]).validate_url(url)


def test_safe_git_subprocess(tmp_path):
    git = GitRepositoryManager(tmp_path, ["github.com"])
    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"abc\n", b"")) as run:
        assert git.run("status") == "abc"
        assert isinstance(run.call_args.args[0], list)
        assert not run.call_args.kwargs.get("shell", False)
        assert "core.hooksPath=/dev/null" in run.call_args.args[0]


def test_actual_git_blob_read_no_execution(tmp_path):
    subprocess.run(["git", "init", "-b", "dev", str(tmp_path)], check=True, capture_output=True)
    task = tmp_path / "release" / "task"
    task.mkdir(parents=True)
    (task / "task.toml").write_text('[metadata]\ncategory="bugfix"\ndifficulty="easy"')
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    git = GitRepositoryManager(tmp_path, ["github.com"])
    sha = git.get_current_commit(tmp_path)
    assert len(sha) == 40
    assert git.get_branch(tmp_path) == "dev"
    assert git.checkout(tmp_path, "dev") == sha
    assert "release/task/task.toml" in git.files(tmp_path, sha)
    assert b"bugfix" in git.read(tmp_path, sha, "release/task/task.toml")


def test_symlink_metadata_is_not_discovered(tmp_path):
    git = GitRepositoryManager(tmp_path, ["github.com"])
    with patch.object(
        git, "run", return_value=b"120000 blob abc\trelease/task.toml\x00100644 blob abc\trelease/file\x00"
    ):
        assert git.files(tmp_path, "a" * 40) == {"release/file"}


def test_invalid_metadata_is_reported():
    class Invalid(FakeGit):
        def read(self, *args):
            return b"invalid ["

    tasks, skipped = ProblemDiscoveryService().discover(Invalid(), Path("unused"), "a" * 40)
    assert not tasks and len(skipped) == 3
