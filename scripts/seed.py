"""Register the initial source without syncing or executing any code."""

from agentbenchx.config import Settings
from agentbenchx.db import database
from agentbenchx.models import ProblemSource
from sqlalchemy import select

_, sessions = database(Settings())
with sessions() as db:
    url = "git@github.com:wongfengchen8-cell/NIFUS-bench.git"
    if not db.scalar(select(ProblemSource).where(ProblemSource.repository_url == url)):
        db.add(
            ProblemSource(id="11111111-1111-4111-8111-111111111111", name="NIFUS Bench", repository_url=url)
        )
        db.commit()
