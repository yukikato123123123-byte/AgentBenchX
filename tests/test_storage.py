import base64
import hashlib
from io import BytesIO
from unittest.mock import Mock, patch

import boto3
import pytest
from agentbenchx.config import Settings
from agentbenchx.main import create_app
from agentbenchx.storage import LocalStorageBackend, S3CompatibleStorageBackend, StorageError, logical_key
from botocore.stub import Stubber
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.test_api import prepare, register


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["upload", "commit"])
def test_storage_failure_rollback_retry_and_private_proxy(app, failure, caplog):
    from agentbenchx.models import Evaluation, RunnerJob
    from pydantic import SecretStr
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session

    objects = {}
    backend = Mock()
    fail_upload = [False]
    writes = [0]

    def put(key, content):
        writes[0] += 1
        if fail_upload[0] and writes[0] == 3:
            raise StorageError("fixture upload failure")
        objects[key] = content

    def read(key):
        if key not in objects:
            raise FileNotFoundError("fixture missing")
        return BytesIO(objects[key])

    backend.put.side_effect = put
    backend.open.side_effect = read
    settings = app.state.test_settings.model_copy(
        update={
            "storage_backend": "s3",
            "s3_bucket": "private-fixture",
            "s3_endpoint_url": "https://account.r2.cloudflarestorage.com",
            "s3_access_key_id": SecretStr("r2-test-access-sentinel"),
            "s3_secret_access_key": SecretStr("r2-test-secret-sentinel"),
        }
    )
    with patch("agentbenchx.main.storage_backend", return_value=backend):
        application = create_app(settings)
        with TestClient(application, headers={"Authorization": "Bearer test-admin"}) as client:
            _, agent, _ = prepare(client, application)
            worker, headers = register(client)
            base = f"/api/v1/workers/{worker['id']}/jobs"
            job = client.post(base + "/claim", headers=headers).json()
            assert all(
                secret not in str(job)
                for secret in (
                    "r2-test-access-sentinel",
                    "r2-test-secret-sentinel",
                    "cloudflarestorage.com",
                    "private-fixture",
                )
            )
            agent_url = base + f"/{job['id']}/agent"
            backend.open.reset_mock()
            assert (
                client.get(
                    agent_url, headers={"Authorization": "", "X-Lease-Token": job["lease_token"]}
                ).status_code
                == 401
            )
            backend.open.assert_not_called()
            downloaded = client.get(agent_url, headers={**headers, "X-Lease-Token": job["lease_token"]})
            assert downloaded.status_code == 200
            assert hashlib.sha256(downloaded.content).hexdigest() == agent["sha256"]
            assert "Location" not in downloaded.headers
            data = {
                "lease_token": job["lease_token"],
                "status": "COMPLETED",
                "metrics": {"runtime_seconds": 1},
            }
            result_url = base + f"/{job['id']}/result"
            if failure == "upload":
                fail_upload[0] = True
                response = client.post(result_url, headers=headers, json=data)
                assert response.status_code == 503
                fail_upload[0] = False
            else:
                with patch.object(
                    Session, "commit", side_effect=IntegrityError("fixture", {}, Exception("fixture"))
                ):
                    response = client.post(result_url, headers=headers, json=data)
                assert response.status_code == 409
            with application.state.sessions() as db:
                assert db.scalar(select(Evaluation).where(Evaluation.job_id == job["id"])) is None
                assert db.get(RunnerJob, job["id"]).status == "CLAIMED"
            result = client.post(result_url, headers=headers, json=data)
            assert result.status_code == 200
            for secret in ("r2-test-access-sentinel", "r2-test-secret-sentinel"):
                assert secret not in result.text + str(downloaded.headers) + caplog.text
            evaluation = result.json()
            with application.state.sessions() as db:
                row = db.get(Evaluation, evaluation["id"])
                artifact_prefix = row.artifact_path
            assert artifact_prefix.startswith(f"evaluations/{evaluation['id']}/")
            artifact_url = f"/api/v1/evaluations/{evaluation['id']}/artifacts/analysis.md"
            assert client.get(artifact_url, headers={"Authorization": ""}).status_code == 401
            assert client.get(artifact_url, headers=headers).status_code == 403
            assert client.get(artifact_url).status_code == 200
            del objects[artifact_prefix + "/analysis.md"]  # Test fixture only; no real storage deletion.
            assert client.get(artifact_url).status_code == 404
            backend.put.reset_mock()
            assert client.post(result_url, headers=headers, json=data).json()["id"] == evaluation["id"]
            backend.put.assert_not_called()


def test_storage_key_and_local_immutability(tmp_path):
    storage = LocalStorageBackend(tmp_path)
    storage.put("agents/a/v/agent.py", b"fixture")
    with storage.open("agents/a/v/agent.py") as stream:
        assert stream.read() == b"fixture"
    with pytest.raises(FileExistsError):
        storage.put("agents/a/v/agent.py", b"changed")
    assert storage.head("agents/a/v/agent.py") == {
        "ContentLength": 7,
        "Metadata": {"sha256": hashlib.sha256(b"fixture").hexdigest()},
    }
    storage.delete("agents/a/v/agent.py")
    storage.delete("agents/a/v/agent.py")
    with pytest.raises(FileNotFoundError):
        storage.open("agents/a/v/agent.py")
    for key in ("/absolute", "../escape", "a/../b", "a//b", "a\\b"):
        with pytest.raises(ValueError):
            logical_key(key)


def test_private_s3_sdk_contract_and_sanitized_failure():
    settings = Settings(
        _env_file=None,
        admin_token="fixture",
        storage_backend="s3",
        s3_bucket="private-fixture",
        s3_access_key_id="fixture",
        s3_secret_access_key="fixture",
    )
    client = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="fixture", aws_secret_access_key="fixture"
    )
    storage = S3CompatibleStorageBackend(settings, client=client)
    with Stubber(client) as stub:
        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": "private-fixture",
                "Key": "agents/a/v/agent.py",
                "Body": b"fixture",
                "IfNoneMatch": "*",
                "Metadata": {"sha256": hashlib.sha256(b"fixture").hexdigest()},
                "ContentType": "text/plain; charset=utf-8",
                "ContentMD5": base64.b64encode(
                    hashlib.md5(b"fixture", usedforsecurity=False).digest()
                ).decode(),
            },
        )
        stub.add_response(
            "head_object",
            {
                "ContentLength": 7,
                "Metadata": {"sha256": hashlib.sha256(b"fixture").hexdigest()},
                "ETag": '"deliberately-not-a-sha256"',
            },
            {"Bucket": "private-fixture", "Key": "agents/a/v/agent.py"},
        )
        stub.add_response(
            "get_object",
            {"Body": BytesIO(b"fixture")},
            {"Bucket": "private-fixture", "Key": "agents/a/v/agent.py"},
        )
        stub.add_client_error(
            "get_object",
            service_error_code="NoSuchKey",
            http_status_code=404,
            expected_params={"Bucket": "private-fixture", "Key": "missing"},
        )
        stub.add_client_error(
            "put_object",
            service_error_code="AccessDenied",
            service_message="secret must not escape",
            http_status_code=403,
        )
        stub.add_response("delete_object", {}, {"Bucket": "private-fixture", "Key": "agents/a/v/agent.py"})
        storage.put("agents/a/v/agent.py", b"fixture")
        assert storage.open("agents/a/v/agent.py").read() == b"fixture"
        with pytest.raises(FileNotFoundError):
            storage.open("missing")
        with pytest.raises(StorageError, match="^Object storage write failed$"):
            storage.put("agents/a/v/agent.py", b"fixture")
        storage.delete("agents/a/v/agent.py")
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fixture.invalid",
        "https://",
        "https://user:secret@fixture.invalid",
        "https://fixture.invalid/bucket",
        "https://fixture.invalid?token=x",
    ],
)
def test_invalid_storage_configuration(endpoint):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            admin_token="fixture",
            storage_backend="s3",
            s3_bucket="fixture",
            s3_endpoint_url=endpoint,
            s3_access_key_id="fixture",
            s3_secret_access_key="fixture",
        )


def test_r2_endpoint_region_and_sdk_configuration():
    settings = Settings(
        _env_file=None,
        admin_token="fixture",
        storage_backend="s3",
        s3_bucket="fixture",
        s3_endpoint_url="https://account.r2.cloudflarestorage.com",
        s3_region="auto",
        s3_access_key_id="fixture",
        s3_secret_access_key="fixture",
    )
    backend = S3CompatibleStorageBackend(settings)
    assert backend.client.meta.endpoint_url == settings.s3_endpoint_url
    assert backend.client.meta.region_name == "auto"
    assert backend.client.meta.config.signature_version == "s3v4"
    assert backend.client.meta.config.request_checksum_calculation == "when_required"
    backend.client.close()


def test_head_missing_and_delete_failure_are_safe():
    settings = Settings(_env_file=None, admin_token="fixture")
    client = boto3.client(
        "s3", region_name="auto", aws_access_key_id="fixture", aws_secret_access_key="fixture"
    )
    backend = S3CompatibleStorageBackend(settings, client)
    backend.bucket = "fixture"
    with Stubber(client) as stub:
        stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
        stub.add_client_error(
            "delete_object",
            service_error_code="AccessDenied",
            service_message="do-not-expose-fixture-secret",
            http_status_code=403,
        )
        with pytest.raises(FileNotFoundError):
            backend.head("missing")
        with pytest.raises(StorageError, match="^Object storage delete failed$"):
            backend.delete("missing")
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    "metadata", [{"ContentLength": 8, "Metadata": {}}, {"ContentLength": 7, "Metadata": {"sha256": "wrong"}}]
)
def test_upload_rejects_bad_integrity(metadata):
    settings = Settings(_env_file=None, admin_token="fixture")
    client = boto3.client(
        "s3", region_name="auto", aws_access_key_id="fixture", aws_secret_access_key="fixture"
    )
    backend = S3CompatibleStorageBackend(settings, client)
    backend.bucket = "fixture"
    with Stubber(client) as stub:
        stub.add_response("put_object", {})
        stub.add_response("head_object", metadata)
        with pytest.raises(StorageError, match="integrity"):
            backend.put("agents/a/v/agent.py", b"fixture")


@pytest.mark.integration
def test_api_proxies_backend_and_idempotent_artifacts(app):
    objects = {}
    backend = Mock()
    backend.put.side_effect = lambda key, value: objects.__setitem__(key, value)
    backend.open.side_effect = lambda key: BytesIO(objects[key])
    with patch("agentbenchx.main.storage_backend", return_value=backend):
        application = create_app(app.state.test_settings)
        with TestClient(application, headers={"Authorization": "Bearer test-admin"}) as client:
            prepare(client, application)
            worker, headers = register(client)
            base = f"/api/v1/workers/{worker['id']}/jobs"
            job = client.post(base + "/claim", headers=headers).json()
            assert client.get(
                base + f"/{job['id']}/agent",
                headers={
                    **headers,
                    "X-Lease-Token": job["lease_token"],
                },
            ).content == next(iter(objects.values()))
            data = {
                "lease_token": job["lease_token"],
                "status": "COMPLETED",
                "metrics": {"runtime_seconds": 1},
            }
            evaluation = client.post(base + f"/{job['id']}/result", headers=headers, json=data).json()
            assert len(objects) == 7
            backend.put.reset_mock()
            assert (
                client.post(base + f"/{job['id']}/result", headers=headers, json=data).json()["id"]
                == evaluation["id"]
            )
            backend.put.assert_not_called()
        # Recreate API, preserving only DB + the independent object backend.
        with TestClient(create_app(app.state.test_settings)) as restarted:
            path = f"/api/v1/evaluations/{evaluation['id']}/artifacts/analysis.md"
            assert restarted.get(path).status_code == 401
            assert restarted.get(path, headers={"Authorization": "Bearer test-admin"}).status_code == 200
