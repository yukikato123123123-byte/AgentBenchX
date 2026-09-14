import random
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

import pytest
from agentbenchx.models import RunnerJob, now
from agentbenchx.services import EvaluationManager
from sqlalchemy import event

from tests.test_api import prepare, register
from workers.evaluator.config import WorkerSettings
from workers.evaluator.scheduling import IdleSchedule


def test_simulated_budget():
    from scripts.simulate_worker_load import simulate

    for rate in (0, 1, 5, 10):
        result = simulate(rate)
        assert result["executions"] == rate * 600
        assert result["artifact_writes"] == rate * 3600
        assert result["redis_with_producer"] == 240 + 11 * rate * 600
        assert result["redis_with_producer"] < 150000
        assert result["http"] < simulate(rate, before=True)["http"]


def test_idle_deadlines_backoff_reset_and_jitter():
    settings = WorkerSettings(_env_file=None, server_url="https://example.invalid")
    schedules = [IdleSchedule(settings, random.Random(seed)) for seed in range(4)]
    assert len({s.startup_delay() for s in schedules}) == 4
    for schedule in schedules:
        schedule.heartbeat_sent(0)
        assert 40.5 <= schedule.next_heartbeat <= 45 < 120 / 2
        for delay in (5, 10, 20, 30, 30):
            schedule.empty(100)
            assert max(5, delay * 0.8) <= schedule.next_poll - 100 <= delay
        assert schedule.heartbeat_due(145)
        schedule.found()
        assert schedule.poll_due(100) and schedule.delay == 5
    legacy = WorkerSettings(_env_file=None, server_url="https://example.invalid", worker_poll_interval=9)
    assert legacy.worker_idle_poll_min_seconds == 9
    slow_legacy = WorkerSettings(
        _env_file=None, server_url="https://example.invalid", worker_poll_interval=60
    )
    assert slow_legacy.worker_idle_poll_max_seconds == 60


@pytest.mark.integration
def test_four_claims_unique_and_result_ownership(client, app):
    _, version, problem = prepare(client, app)
    for _ in range(3):
        assert (
            client.post(
                "/api/v1/submissions",
                json={
                    "agent_version_id": version["id"],
                    "problem_version_ids": [problem["id"]],
                },
            ).status_code
            == 201
        )
    workers = [register(client, f"Mac #{n}") for n in range(4)]
    with ThreadPoolExecutor(4) as pool:
        responses = list(
            pool.map(lambda w: client.post(f"/api/v1/workers/{w[0]['id']}/jobs/claim", headers=w[1]), workers)
        )
    assert all(r.status_code == 200 for r in responses)
    jobs = [r.json() for r in responses]
    assert len({j["id"] for j in jobs}) == 4
    a, b = workers[:2]
    response = client.post(
        f"/api/v1/workers/{a[0]['id']}/jobs/{jobs[1]['id']}/result",
        headers=a[1],
        json={
            "lease_token": jobs[1]["lease_token"],
            "status": "COMPLETED",
            "metrics": {"runtime_seconds": 1},
        },
    )
    assert response.status_code == 403


@pytest.mark.integration
@pytest.mark.parametrize("running", [False, True])
def test_crash_recovery_and_attempt_limit(client, app, running):
    prepare(client, app)
    worker, headers = register(client)
    base = f"/api/v1/workers/{worker['id']}/jobs"
    job = client.post(base + "/claim", headers=headers).json()
    if running:
        assert (
            client.post(
                base + f"/{job['id']}/heartbeat",
                headers=headers,
                json={
                    "lease_token": job["lease_token"],
                    "event": "agent.started",
                },
            ).status_code
            == 200
        )
    with app.state.sessions() as db:
        row = db.get(RunnerJob, job["id"])
        row.lease_expires_at = now() - timedelta(seconds=1)
        db.commit()
        service = EvaluationManager(db, app.state.test_settings, app.state.queue)
        service.reap()
        assert db.get(RunnerJob, job["id"]).status == "TIMEOUT"
    retry = client.post(base + "/claim", headers=headers).json()
    assert retry["attempt"] == 2
    with app.state.sessions() as db:
        row = db.get(RunnerJob, retry["id"])
        row.attempt = app.state.test_settings.max_attempts
        row.lease_expires_at = now() - timedelta(seconds=1)
        db.commit()
        EvaluationManager(db, app.state.test_settings, app.state.queue).reap()
        assert db.get(RunnerJob, retry["id"]).status == "FAILED"
    assert client.post(base + "/claim", headers=headers).json() is None


@pytest.mark.integration
def test_measured_sql_and_transition_events(client, app):
    prepare(client, app)
    worker, headers = register(client)
    base = f"/api/v1/workers/{worker['id']}"
    engine = app.state.sessions.kw["bind"]
    counts = Counter()

    def sql(conn, cursor, statement, params, context, executemany):
        counts[statement.split()[0].upper()] += 1

    def commit(conn):
        counts["COMMIT"] += 1

    event.listen(engine, "before_cursor_execute", sql)
    event.listen(engine, "commit", commit)
    try:
        with patch.object(app.state.queue, "publish") as publish:
            response = client.post(base + "/heartbeat", headers=headers, json={})
            assert response.status_code == 200
            assert counts == {"SELECT": 2, "UPDATE": 1, "COMMIT": 1}
            publish.assert_not_called()
            counts.clear()
            with patch.object(EvaluationManager, "reap", side_effect=AssertionError("claim must not reap")):
                job = client.post(base + "/jobs/claim", headers=headers).json()
            assert counts == {"SELECT": 6, "UPDATE": 2, "COMMIT": 1}
            counts.clear()
            assert (
                client.get(
                    base + f"/jobs/{job['id']}/agent",
                    headers={
                        **headers,
                        "X-Lease-Token": job["lease_token"],
                    },
                ).status_code
                == 200
            )
            assert counts == {"SELECT": 4}
            counts.clear()
            publish.reset_mock()
            for _ in range(3):
                assert (
                    client.post(
                        base + f"/jobs/{job['id']}/heartbeat",
                        headers=headers,
                        json={
                            "lease_token": job["lease_token"],
                            "event": "agent.started",
                        },
                    ).status_code
                    == 200
                )
            assert counts == {"SELECT": 6, "UPDATE": 6, "COMMIT": 3}
            assert publish.call_count == 1
            counts.clear()
            data = {
                "lease_token": job["lease_token"],
                "status": "COMPLETED",
                "metrics": {"runtime_seconds": 1},
                "tests": [{"name": "fixture", "status": "PASSED"}],
            }
            result = client.post(base + f"/jobs/{job['id']}/result", headers=headers, json=data)
            assert result.status_code == 200
            assert counts == {"SELECT": 5, "INSERT": 5, "UPDATE": 2, "COMMIT": 1}
            counts.clear()
            publish.reset_mock()
            assert (
                client.post(base + f"/jobs/{job['id']}/result", headers=headers, json=data).json()["id"]
                == result.json()["id"]
            )
            assert counts == {"SELECT": 3}
            publish.assert_not_called()
            counts.clear()
            assert client.post(base + "/jobs/claim", headers=headers).json() is None
            assert counts == {"SELECT": 3, "COMMIT": 1}
            counts.clear()
            with app.state.sessions() as db:
                EvaluationManager(db, app.state.test_settings, app.state.queue).reap()
            assert counts == {"SELECT": 2, "COMMIT": 2}
    finally:
        event.remove(engine, "before_cursor_execute", sql)
        event.remove(engine, "commit", commit)


@pytest.mark.integration
def test_slow_storage_cannot_accept_expired_result(client, app):
    from agentbenchx.models import Evaluation
    from sqlalchemy import select

    prepare(client, app)
    worker, headers = register(client)
    base = f"/api/v1/workers/{worker['id']}/jobs"
    job = client.post(base + "/claim", headers=headers).json()
    clock = [now()]

    def store(*args, **kwargs):
        clock[0] += timedelta(seconds=121)
        return "evaluations/fixture/problem"

    with (
        patch("agentbenchx.services.now", side_effect=lambda: clock[0]),
        patch("agentbenchx.services.FailureAnalysisService.collect_artifacts", side_effect=store),
    ):
        response = client.post(
            base + f"/{job['id']}/result",
            headers=headers,
            json={
                "lease_token": job["lease_token"],
                "status": "COMPLETED",
                "metrics": {"runtime_seconds": 1},
            },
        )
    assert response.status_code == 409
    with app.state.sessions() as db:
        assert db.scalar(select(Evaluation).where(Evaluation.job_id == job["id"])) is None
