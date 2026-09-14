"""Opt-in metadata-only verification against an actual Git repository."""

import os
from pathlib import Path

import pytest
from agentbenchx.git_sources import GitRepositoryManager, ProblemDiscoveryService


@pytest.mark.nifus
def test_real_source():
    path = os.getenv("NIFUS_REPOSITORY")
    if not path:
        pytest.skip("Set NIFUS_REPOSITORY to a real local Git repository")
    git = GitRepositoryManager(Path("storage"), ["github.com"])
    sha = git.get_current_commit(Path(path))
    tasks, skipped = ProblemDiscoveryService().discover(git, Path(path), sha)
    assert len(sha) in (40, 64)
    assert tasks
    assert all(task["category"] != "GIS" for task in tasks)
    assert all(task["metadata"]["format"] == "harbor" for task in tasks)
