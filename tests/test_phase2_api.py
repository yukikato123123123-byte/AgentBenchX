from datetime import timedelta

import pytest
from agentbenchx.models import Worker, now
from agentbenchx.services import EvaluationManager

from tests.test_api import prepare, register

pytestmark = pytest.mark.integration


def test_null_usage_execution_evidence_and_artifacts(client, app):
    prepare(client, app)
    worker, headers = register(client)
    base = f"/api/v1/workers/{worker['id']}/jobs"
    job = client.post(base + "/claim", headers=headers).json()
    process = {
        "exit_code": 1,
        "started_at": "2026-09-14T00:00:00Z",
        "finished_at": "2026-09-14T00:00:01Z",
        "runtime_seconds": 1,
    }
    result = {
        "lease_token": job["lease_token"],
        "status": "FAILED",
        "failure_type": "AGENT_FAILURE",
        "metrics": {
            "runtime_seconds": 1,
            "input_tokens": None,
            "output_tokens": None,
            "llm_cost": None,
            "compute_cost": None,
        },
        "execution": {"agent": process, "error": "controlled failure"},
        "stderr": "actual fixture evidence",
    }
    response = client.post(base + f"/{job['id']}/result", headers=headers, json=result)
    assert response.status_code == 200, response.text
    evaluation = response.json()
    assert evaluation["metrics"]["total_cost"] is None
    assert evaluation["metrics"]["execution"]["agent"]["exit_code"] == 1
    summary = client.get("/api/v1/dashboard").json()["summary"]
    assert (
        summary["total_tokens"] is None and summary["total_cost"] is None and summary["average_cost"] is None
    )
    report = client.get(f"/api/v1/evaluations/{evaluation['id']}/artifacts/analysis.md").text
    assert (
        worker["id"] in report
        and job["problem_version"]["commit_sha"] in report
        and '"exit_code": 1' in report
    )
    assert "individual-secret" not in report and worker["token"] not in report
    assert (
        client.post(
            f"/api/v1/workers/{worker['id']}/heartbeat", headers=headers, json={"status": "OFFLINE"}
        ).json()["status"]
        == "OFFLINE"
    )


def test_reaper_emits_offline_event(client, app):
    from unittest.mock import patch

    worker, _ = register(client)
    with app.state.sessions() as db:
        row = db.get(Worker, worker["id"])
        row.last_heartbeat = now() - timedelta(seconds=200)
        db.commit()
        with patch.object(app.state.queue, "publish") as publish:
            EvaluationManager(db, app.state.test_settings, app.state.queue).reap()
            publish.assert_any_call("worker.status", worker_id=worker["id"], status="OFFLINE")


def test_worker_events_traverse_real_websocket(client, app):
    prepare(client, app)
    with client.websocket_connect("/api/v1/events") as ws:
        ws.send_json({"token": "test-admin"})
        assert ws.receive_json()["type"] == "connected"
        worker, headers = register(client)
        base = f"/api/v1/workers/{worker['id']}/jobs"
        job = client.post(base + "/claim", headers=headers).json()
        for event in ["job.started", "agent.started", "agent.finished", "test.started", "test.finished"]:
            response = client.post(
                base + f"/{job['id']}/heartbeat",
                headers=headers,
                json={"lease_token": job["lease_token"], "event": event},
            )
            assert response.status_code == 200
        response = client.post(
            base + f"/{job['id']}/result",
            headers=headers,
            json={
                "lease_token": job["lease_token"],
                "status": "COMPLETED",
                "metrics": {"runtime_seconds": 1},
                "tests": [{"name": "trusted fixture", "status": "PASSED"}],
            },
        )
        assert response.status_code == 200
        seen = set()
        for _ in range(10):
            seen.add(ws.receive_json()["type"])
            if "evaluation.completed" in seen:
                break
        assert {
            "worker.status",
            "job.started",
            "agent.started",
            "agent.finished",
            "test.started",
            "test.finished",
            "evaluation.completed",
        } <= seen
