"""Run tests in disposable schemas on the configured local PostgreSQL instance."""

import os
import subprocess
import sys

from agentbenchx.config import Settings

settings = Settings()
env = {
    **os.environ,
    "TEST_DATABASE_URL": settings.database_url,
    "TEST_REDIS_URL": settings.redis_url.rsplit("/", 1)[0] + "/15",
}
raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-q", *sys.argv[1:]], env=env))
