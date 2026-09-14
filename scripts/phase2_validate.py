"""Control-plane-only driver. Uploads bytes; actual agent/test execution is exclusively on Mac #1."""

import argparse
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from agentbenchx.config import Settings
from websockets.sync.client import connect

ARTIFACTS = ["agent_output.log", "stdout.log", "stderr.log", "test_result.json", "patch.diff", "analysis.md"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--timeout", type=int, default=3600, help="Maximum wait per evaluation, including builds"
    )
    args = parser.parse_args()
    settings = Settings()
    selected = json.loads(Path("docs/phase2-problem.json").read_text())
    output = (
        settings.storage_root / "phase2-verification" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    with httpx.Client(
        base_url=args.server_url, headers={"Authorization": f"Bearer {settings.admin_token}"}, timeout=30
    ) as client:
        workers_response = client.get("/api/v1/workers")
        workers_response.raise_for_status()
        workers = [
            w
            for w in workers_response.json()
            if w["status"] == "ONLINE" and w["platform"] == "darwin" and w["version"] == "0.2.0"
        ]
        if len(workers) != 1:
            raise SystemExit("Exactly one ONLINE Phase 2 Mac worker is required. No jobs were submitted.")
        if any(
            w["status"] in {"ONLINE", "BUSY"} and w["id"] != workers[0]["id"] for w in workers_response.json()
        ):
            raise SystemExit("Stop other workers before the Mac #1 verification. No jobs were submitted.")
        versions = client.get(f"/api/v1/problems/{selected['problem_id']}/versions")
        versions.raise_for_status()
        version = next((v for v in versions.json() if v["id"] == selected["problem_version_id"]), None)
        if not version or version["commit_sha"] != selected["commit_sha"]:
            raise SystemExit("Selected immutable version is unavailable or mismatched")
        queue = client.get("/api/v1/jobs", params={"limit": 1000})
        queue.raise_for_status()
        if any(job["status"] in {"QUEUED", "CLAIMED", "RUNNING"} for job in queue.json()):
            raise SystemExit("Clear or finish existing jobs before this controlled run; no jobs submitted")
        output.mkdir(parents=True)
        (output / "selection.json").write_text(json.dumps(selected, indent=2))
        (output / "worker.json").write_text(json.dumps(workers[0], indent=2))
        stop = threading.Event()
        ready = threading.Event()
        event_error = []

        def events():
            try:
                url = args.server_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
                with (
                    connect(url + "/api/v1/events", open_timeout=10) as ws,
                    (output / "events.jsonl").open("w") as stream,
                ):
                    ws.send(json.dumps({"token": settings.admin_token}))
                    initial = json.loads(ws.recv(timeout=10))
                    if initial["type"] != "connected":
                        raise RuntimeError("WebSocket authentication failed")
                    ready.set()
                    while not stop.is_set():
                        try:
                            message = ws.recv(timeout=1)
                        except TimeoutError:
                            continue
                        stream.write(message + "\n")
                        stream.flush()
            except Exception:
                event_error.append("WebSocket evidence capture failed")
                ready.set()

        thread = threading.Thread(target=events, daemon=True)
        thread.start()
        ready.wait(15)
        if event_error or not ready.is_set():
            stop.set()
            raise SystemExit("Cannot capture WebSocket evidence; no jobs submitted")
        try:
            for stage, agent_kind, expected in [
                ("success", "success", "COMPLETED"),
                ("failure", "failure", "FAILED"),
                ("timeout", "timeout", "TIMEOUT"),
                ("recovery", "success", "COMPLETED"),
            ]:
                folder = output / stage
                folder.mkdir()
                agent = client.post(
                    "/api/v1/agents",
                    json={
                        "name": f"Phase 2 controlled {stage}",
                        "description": "Single pinned joserfc task; infrastructure validation only",
                    },
                )
                agent.raise_for_status()
                source = Path("workers/evaluator/validation_agents") / agent_kind / "agent.py"
                uploaded = client.post(
                    f"/api/v1/agents/{agent.json()['id']}/versions",
                    files={"file": ("agent.py", source.read_bytes())},
                )
                uploaded.raise_for_status()
                submitted = client.post(
                    "/api/v1/submissions",
                    json={
                        "agent_version_id": uploaded.json()["id"],
                        "problem_version_ids": [selected["problem_version_id"]],
                    },
                )
                submitted.raise_for_status()
                submission = submitted.json()[0]
                (folder / "input.json").write_text(
                    json.dumps({"agent_version": uploaded.json(), "submission": submission}, indent=2)
                )
                deadline = time.monotonic() + args.timeout
                evaluation = None
                while time.monotonic() < deadline:
                    response = client.get("/api/v1/evaluations", params={"limit": 1000})
                    response.raise_for_status()
                    evaluation = next(
                        (e for e in response.json() if e["submission_id"] == submission["id"]), None
                    )
                    if evaluation:
                        break
                    time.sleep(2)
                if evaluation is None:
                    raise RuntimeError(
                        f"{stage}: no result before deadline; inspect queued job and worker logs"
                    )
                detail = client.get(f"/api/v1/evaluations/{evaluation['id']}")
                detail.raise_for_status()
                (folder / "evaluation.json").write_text(json.dumps(detail.json(), indent=2))
                for name in ARTIFACTS:
                    artifact = client.get(f"/api/v1/evaluations/{evaluation['id']}/artifacts/{name}")
                    artifact.raise_for_status()
                    (folder / name).write_bytes(artifact.content)
                if evaluation["status"] != expected or evaluation["worker_id"] != workers[0]["id"]:
                    raise RuntimeError(
                        f"{stage}: expected {expected} on Mac #1, received {evaluation['status']}"
                    )
                if any(
                    evaluation["metrics"].get(k) is not None
                    for k in ("input_tokens", "output_tokens", "llm_cost", "compute_cost")
                ):
                    raise RuntimeError("Controlled agents expose no usage data; expected null metrics")
                if stage == "success" and (
                    not evaluation["metrics"]["tests_passed"] or not (folder / "patch.diff").stat().st_size
                ):
                    raise RuntimeError("Success requires actual passing tests and a nonempty patch")
                expected_failure = {"failure": "TEST_FAILURE", "timeout": "TIMEOUT"}.get(stage)
                if evaluation["failure_type"] != expected_failure:
                    raise RuntimeError(f"{stage}: unexpected failure classification")
                returned_worker = client.get(f"/api/v1/workers/{workers[0]['id']}")
                returned_worker.raise_for_status()
                if returned_worker.json()["status"] != "ONLINE":
                    raise RuntimeError(f"{stage}: worker did not recover to ONLINE")
                print(f"{stage}: {evaluation['id']} {evaluation['status']}")
            if event_error:
                raise RuntimeError(event_error[0])
            (output / "VERIFIED.json").write_text(
                json.dumps({"mac_1_verified": True, "worker_id": workers[0]["id"]})
            )
        finally:
            stop.set()
            thread.join(timeout=12)
    print(f"Evidence saved under {output}")


if __name__ == "__main__":
    main()
