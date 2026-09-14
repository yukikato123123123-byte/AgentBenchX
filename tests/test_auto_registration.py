"""Exercise the actual worker HTTP client against the existing API, without Mac execution."""

from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_api import prepare
from workers.evaluator.client import WorkerClient
from workers.evaluator.config import WorkerSettings
from workers.evaluator.main import identity

pytestmark = pytest.mark.integration


def test_first_connection_restart_and_independent_workers(app, client, tmp_path):
    transports = []

    def connect(url, token=None):
        worker = WorkerClient(url, token)
        worker.http.close()
        worker.http = TestClient(
            app, base_url=url, headers={"Authorization": f"Bearer {token}"} if token else {}
        )
        transports.append(worker)
        return worker

    def config(folder):
        folder.mkdir(exist_ok=True)
        return WorkerSettings(_env_file=None, server_url="http://localhost:8000", workspace_root=folder)

    try:
        with patch("workers.evaluator.main.WorkerClient", side_effect=connect):
            first, a = identity(config(tmp_path / "a"))
            assert a.heartbeat(first)["status"] == "ONLINE"
            assert a.claim(first) is None
            restarted, resumed = identity(config(tmp_path / "a"))
            assert restarted == first
            assert resumed.http.headers["Authorization"] == a.http.headers["Authorization"]
            second, b = identity(config(tmp_path / "b"))
            assert second != first
            assert b.http.headers["Authorization"] != a.http.headers["Authorization"]
            assert len(client.get("/api/v1/workers").json()) == 2
            assert b.heartbeat(second)["status"] == "ONLINE"
            prepare(client, app)
            assignment = resumed.claim(first)
            assert assignment["lease_token"]
            assert b.claim(second) is None
            resumed.job_heartbeat(first, assignment["id"], assignment["lease_token"], "test.progress")
            with pytest.raises(httpx.HTTPStatusError) as wrong_identity:
                b.heartbeat(first)
            assert wrong_identity.value.response.status_code == 403
            with pytest.raises(httpx.HTTPStatusError) as wrong_lease:
                resumed.job_heartbeat(first, assignment["id"], "invalid-lease", "test.progress")
            assert wrong_lease.value.response.status_code == 403
            with pytest.raises(ValueError, match="another SERVER_URL"):
                identity(config(tmp_path / "a").model_copy(update={"server_url": "http://different.invalid"}))
    finally:
        for worker in transports:
            worker.close()


def test_registration_is_open_but_other_authentication_is_preserved(app):
    with TestClient(app) as anonymous:
        response = anonymous.post(
            "/api/v1/workers/register",
            json={"name": "Trusted worker", "hostname": "fixture", "platform": "darwin", "version": "0.2.0"},
        )
        assert response.status_code == 201
        worker = response.json()
        assert worker["token"] and "token_hash" not in worker
        assert anonymous.get("/api/v1/workers").status_code == 401
        assert anonymous.get("/api/v1/dashboard").status_code == 401
        assert anonymous.post(f"/api/v1/workers/{worker['id']}/heartbeat", json={}).status_code == 401
        assert anonymous.post(f"/api/v1/workers/{worker['id']}/jobs/claim", json={}).status_code == 401
