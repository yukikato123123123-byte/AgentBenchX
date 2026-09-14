"""Copied into containers. Never import or launch this module on the control plane."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


def git(*args):
    return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-c", "safe.directory=*", *args])


def main():
    mode = sys.argv[1]
    if mode == "agent":
        spec = importlib.util.spec_from_file_location("uploaded_agent", "/runner/agent.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        payload = json.loads(Path("/runner/input.json").read_text())
        payload["workspace"] = os.getcwd()
        patch = module.agent_main(payload)
        if not isinstance(patch, str):
            raise TypeError("agent_main must return a unified diff string")
        if not patch:
            git("add", "-A")
            patch = git("diff", "--cached", "--binary", "--full-index", "HEAD").decode()
        Path("/tmp/abx-patch.diff").write_text(patch)
    elif mode == "test":
        patch = Path("/candidate/patch.diff").read_bytes()
        # Check/apply only inside the fresh verifier container. Default Git rejects escaping paths.
        if patch:
            subprocess.run(
                ["git", "-c", "safe.directory=*", "apply", "--check", "/candidate/patch.diff"], check=True
            )
            subprocess.run(["git", "-c", "safe.directory=*", "apply", "/candidate/patch.diff"], check=True)
        Path("/logs/agent").mkdir(parents=True, exist_ok=True)
        Path("/logs/agent/patch.diff").write_bytes(patch)
        command = json.loads(Path("/candidate/test-command.json").read_text())
        if not command or not all(isinstance(arg, str) and "\x00" not in arg for arg in command):
            raise ValueError("Test command must be an argument array")
        # Pass the exact recorded argv; no shell expansion or pytest substitution.
        os.execvp(command[0], command)
    else:
        raise ValueError("Unknown harness mode")


if __name__ == "__main__":
    main()
