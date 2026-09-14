import os
from uuid import uuid4

import pytest
from agentbenchx.config import Settings
from agentbenchx.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


@pytest.fixture
def app(tmp_path):
    """Each integration test gets a disposable PostgreSQL schema, never truncates application data."""
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL/Redis integration tests")
    schema = "test_" + uuid4().hex
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    test_url = (
        make_url(url)
        .update_query_dict({"options": f"-csearch_path={schema}"})
        .render_as_string(hide_password=False)
    )
    settings = Settings(
        database_url=test_url,
        redis_url=os.getenv("TEST_REDIS_URL", "redis://localhost:56379/15"),
        admin_token="test-admin",
        storage_root=tmp_path,
    )
    from alembic import command
    from alembic.config import Config

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = test_url
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL")
        else:
            os.environ["DATABASE_URL"] = previous
    application = create_app(settings)
    application.state.test_settings = settings
    yield application
    application.state.queue.redis.close()
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    engine.dispose()


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer test-admin"
        yield client
