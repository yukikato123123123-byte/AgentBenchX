from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

import pytest
from agentbenchx.models import AgentVersion, Evaluation, RunnerJob, now
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from tests.test_discovery import FakeGit

pytestmark = pytest.mark.integration


def register(client, name="Mac #1"):
    response = client.post(
        "/api/v1/workers/register",
        headers={"Authorization": ""},
        json={
            "name": name,
            "hostname": "mac.local",
            "platform": "darwin",
            "version": "0.1",
            "capabilities": ["harbor"],
        },
    )
    assert response.status_code == 201, response.text
    w = response.json()
    return w, {"Authorization": f"Bearer {w['token']}"}


def prepare(client, app):
    agent = client.post("/api/v1/agents", json={"name": "test-agent"}).json()
    response = client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        files={"file": ("agent.py", b"raise RuntimeError('must never run')\n")},
    )
    assert response.status_code == 201, response.text
    version = response.json()
    source = client.post(
        "/api/v1/problem-sources",
        json={"name": "Test source", "repository_url": "git@github.com:example/bench.git"},
    ).json()
    fake = FakeGit()
    git = app.state.git
    with (
        patch.object(git, "clone", return_value=git.root),
        patch.object(git, "get_branch", return_value="release"),
        patch.object(git, "checkout", return_value="a" * 40),
        patch.object(git, "pin"),
        patch.object(git, "files", side_effect=fake.files),
        patch.object(git, "read", side_effect=fake.read),
    ):
        response = client.post(f"/api/v1/problem-sources/{source['id']}/sync")
        assert response.status_code == 200, response.text
        assert response.json()["last_commit_sha"] == "a" * 40
        assert response.json()["sync_report"]["versions_created"] == 2
        response = client.post(f"/api/v1/problem-sources/{source['id']}/sync")
        assert response.json()["sync_report"]["versions_created"] == 0
    problems = client.get("/api/v1/problems").json()
    pv = client.get(f"/api/v1/problems/{problems[0]['id']}/versions").json()[0]
    assert pv["branch"] == "release" and pv["commit_sha"] == "a" * 40
    submissions = client.post(
        "/api/v1/submissions", json={"agent_version_id": version["id"], "problem_version_ids": [pv["id"]]}
    )
    assert submissions.status_code == 201, submissions.text
    return agent, version, pv


def test_health_auth_and_validation(client):
    assert client.get("/api/v1/health").json() == {"status": "ok", "database": True, "redis": True}
    assert client.get("/api/v1/agents", headers={"Authorization": ""}).status_code == 401
    assert client.post("/api/v1/agents", json={"name": ""}).status_code == 422
    assert (
        client.post(
            "/api/v1/problem-sources", json={"name": "bad", "repository_url": "file:///etc"}
        ).status_code
        == 422
    )
    assert client.get("/api/v1/agents/missing").status_code == 404
    assert client.get("/api/v1/agents?limit=-1").status_code == 422


def test_full_control_plane_flow(client, app):
    agent, version, pv = prepare(client, app)
    assert version["version"] == 1 and len(version["sha256"]) == 64
    assert "storage_path" not in version
    worker, headers = register(client)
    assert (
        client.post(f"/api/v1/workers/{worker['id']}/heartbeat", headers=headers, json={}).json()["status"]
        == "ONLINE"
    )
    base = f"/api/v1/workers/{worker['id']}/jobs"
    job = client.post(base + "/claim", headers=headers).json()
    assert job["problem_version"]["commit_sha"] == "a" * 40
    assert "token_hash" not in str(job) and "PRIVATE KEY" not in str(job)
    assert client.post(base + "/claim", headers=headers).status_code == 409
    lease = job["lease_token"]
    assert client.get(
        base + f"/{job['id']}/agent", headers={**headers, "X-Lease-Token": lease}
    ).content.startswith(b"raise RuntimeError")
    heartbeat = client.post(
        base + f"/{job['id']}/heartbeat",
        headers=headers,
        json={"lease_token": lease, "event": "test.started"},
    )
    assert heartbeat.json()["status"] == "RUNNING"
    data = {
        "lease_token": lease,
        "status": "FAILED",
        "failure_type": "TEST_FAILURE",
        "metrics": {
            "runtime_seconds": 12.5,
            "input_tokens": 100,
            "output_tokens": 50,
            "llm_cost": 0.01,
            "compute_cost": 0.002,
        },
        "tests": [{"name": "test_one", "status": "PASSED"}, {"name": "test_two", "status": "FAILED"}],
        "stderr": "assertion failed",
        "patch": "diff --git a/x b/x\n",
    }
    response = client.post(base + f"/{job['id']}/result", headers=headers, json=data)
    assert response.status_code == 200, response.text
    evaluation = response.json()
    assert evaluation["metrics"]["total_tokens"] == 150
    assert evaluation["metrics"]["total_cost"] == 0.012
    assert evaluation["metrics"]["score"] == 0.5
    again = client.post(base + f"/{job['id']}/result", headers=headers, json=data)
    assert again.json()["id"] == evaluation["id"]
    detail = client.get(f"/api/v1/evaluations/{evaluation['id']}").json()
    assert detail["problem_version"]["commit_sha"] == "a" * 40
    assert detail["job"]["attempt"] == 1
    assert len(detail["tests"]) == 2
    assert (
        "assertion failed" in client.get(f"/api/v1/evaluations/{evaluation['id']}/artifacts/stderr.log").text
    )
    assert client.get(f"/api/v1/evaluations/{evaluation['id']}/artifacts/private.key").status_code == 404
    assert client.get("/api/v1/dashboard").json()["summary"]["failed"] == 1
    with app.state.sessions() as db:
        assert len(db.scalars(select(Evaluation)).all()) == 1


def test_upload_rejection_and_immutable_versions(client, app):
    agent, version, _ = prepare(client, app)
    path = f"/api/v1/agents/{agent['id']}/versions"
    for filename, content in [
        ("evil.py", b"pass"),
        ("agent.py", b"bad syntax !"),
        ("agent.py", b""),
        ("agent.py", b"x" * 2_000_001),
    ]:
        assert client.post(path, files={"file": (filename, content)}).status_code == 422
    v2 = client.post(path, files={"file": ("agent.py", b"pass")}).json()
    assert v2["version"] == 2 and v2["sha256"] != version["sha256"]
    with app.state.sessions() as db:
        with pytest.raises(DBAPIError):
            db.execute(text("UPDATE agent_versions SET version=99 WHERE id=:id"), {"id": version["id"]})
            db.commit()
        db.rollback()
        assert db.get(AgentVersion, version["id"]).version == 1


def test_concurrent_claim_single_winner(client, app):
    prepare(client, app)
    workers = [register(client, f"Mac #{i}") for i in range(4)]

    def claim(item):
        w, headers = item
        # Reuse the one application lifespan; nested clients would close shared Redis pools.
        return client.post(f"/api/v1/workers/{w['id']}/jobs/claim", headers=headers).json()

    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(claim, workers))
    assert sum(c is not None for c in claims) == 1


def test_expired_lease_retries_and_fences_stale_worker(client, app):
    prepare(client, app)
    worker, headers = register(client)
    base = f"/api/v1/workers/{worker['id']}/jobs"
    job = client.post(base + "/claim", headers=headers).json()
    with app.state.sessions() as db:
        record = db.get(RunnerJob, job["id"])
        record.lease_expires_at = now() - timedelta(seconds=1)
        db.commit()
    from agentbenchx.services import EvaluationManager
    with app.state.sessions() as db:
        EvaluationManager(db, app.state.test_settings, app.state.queue).reap()
    second, second_headers = register(client, "Mac #2")
    retry = client.post(f"/api/v1/workers/{second['id']}/jobs/claim", headers=second_headers).json()
    assert retry["attempt"] == 2 and retry["id"] != job["id"]
    assert (
        client.post(
            base + f"/{job['id']}/heartbeat", headers=headers, json={"lease_token": job["lease_token"]}
        ).status_code
        == 409
    )
    assert (
        client.post(
            base + f"/{job['id']}/result",
            headers=headers,
            json={
                "lease_token": job["lease_token"],
                "status": "COMPLETED",
                "metrics": {"runtime_seconds": 1},
            },
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/v1/workers/{second['id']}/jobs/{retry['id']}/heartbeat",
            headers=headers,
            json={"lease_token": retry["lease_token"]},
        ).status_code
        == 403
    )


def test_websocket_events(client, app):
    with client.websocket_connect("/api/v1/events") as ws:
        ws.send_json({"token": "test-admin"})
        assert ws.receive_json()["type"] == "connected"
        app.state.queue.publish("test.progress", job_id="test")
        assert ws.receive_json()["type"] == "test.progress"


def test_draining_worker_cannot_claim(client, app):
    prepare(client, app)
    worker, headers = register(client)
    path = f"/api/v1/workers/{worker['id']}"
    assert (
        client.post(path + "/heartbeat", headers=headers, json={"status": "DRAINING"}).json()["status"]
        == "DRAINING"
    )
    assert client.post(path + "/jobs/claim", headers=headers).status_code == 409


def test_redis_queue_outage_does_not_lose_jobs(client, app):
    from redis.exceptions import ConnectionError

    with (
        patch.object(app.state.queue.redis, "zadd", side_effect=ConnectionError),
        patch.object(app.state.queue.redis, "publish", side_effect=ConnectionError),
    ):
        prepare(client, app)
    worker, headers = register(client)
    assert (
        client.post(f"/api/v1/workers/{worker['id']}/jobs/claim", headers=headers).json()["status"]
        == "CLAIMED"
    )


def test_result_validation(client, app):
    prepare(client, app)
    worker, headers = register(client)
    path = f"/api/v1/workers/{worker['id']}/jobs"
    job = client.post(path + "/claim", headers=headers).json()
    result = {"lease_token": job["lease_token"], "status": "COMPLETED", "metrics": {"runtime_seconds": -1}}
    assert client.post(path + f"/{job['id']}/result", headers=headers, json=result).status_code == 422
    result["metrics"]["runtime_seconds"] = 1
    result["tests"] = [{"name": "failed", "status": "FAILED"}]
    assert client.post(path + f"/{job['id']}/result", headers=headers, json=result).status_code == 422


def test_source_failure_persists_status(client, app):
    from agentbenchx.git_sources import SourceError

    source = client.post(
        "/api/v1/problem-sources",
        json={"name": "Unavailable", "repository_url": "git@github.com:example/private.git"},
    ).json()
    with patch.object(app.state.git, "clone", side_effect=SourceError("Git operation failed")):
        assert client.post(f"/api/v1/problem-sources/{source['id']}/sync").status_code == 422
    assert client.get(f"/api/v1/problem-sources/{source['id']}").json()["sync_status"] == "FAILED"
