"""Controlled infrastructure agent for the pinned joserfc task; not a general coding agent."""

import difflib
from pathlib import Path


def agent_main(input: dict) -> str:
    relative = "src/joserfc/_rfc7515/json.py"
    path = Path(input["workspace"]) / relative
    before = path.read_text()
    needle = "    assert obj.signature is not None\n"
    if before.count(needle) != 1:
        raise ValueError("Expected pinned joserfc assertion was not found exactly once")
    after = before.replace(needle, "    if obj.signature is None:\n        return False\n")
    return f"diff --git a/{relative} b/{relative}\n" + "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
        )
    )
