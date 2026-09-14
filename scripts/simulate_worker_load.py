"""Virtual-time capacity model using the production idle scheduler (no API/worker execution).

Fixture: one job every 3600/rate seconds per worker, 60-second lease lifetime,
one test/result, seven lease calls (five stages + t=20,40), no retries or latency.
SQL costs are asserted against real PostgreSQL in test_optimization.py; exclude
pre-ping, transaction control and wire framing. This is not a production benchmark.
"""

import json
import random
from collections import Counter

from workers.evaluator.config import WorkerSettings
from workers.evaluator.scheduling import IdleSchedule


def simulate(rate=0, workers=4, days=30, before=False):
    counts = Counter({key: 0 for key in ("executions", "artifact_writes", "results", "lease_heartbeats")})
    settings = WorkerSettings(_env_file=None, server_url="https://fixture.invalid")

    def request(sql, commits=1, redis=0):
        counts["http"] += 1
        counts["sql"] += sql
        counts["transactions"] += commits
        counts["redis"] += redis

    for worker in range(workers):
        request(1, redis=1)  # First enrollment only, identity reused on subsequent days.
        for day in range(days):
            schedule = IdleSchedule(settings, random.Random(worker * 1000 + day))
            t = 0.0 if before else schedule.startup_delay()
            completed = 0
            first_heartbeat = True
            while t < 18000:
                if before or schedule.heartbeat_due(t):
                    request(4 if before else 3, redis=int(before or (day > 0 and first_heartbeat)))
                    counts["idle_heartbeats"] += 1
                    first_heartbeat = False
                    schedule.heartbeat_sent(t)
                if not before and not schedule.poll_due(t):
                    t += schedule.wait_seconds(t)
                    continue
                available = rate and completed < 5 * rate and t >= completed * 3600 / rate
                counts["claims"] += 1
                if not available:
                    request(6 if before else 3, commits=3 if before else 1)
                    counts["empty_claims"] += 1
                    schedule.empty(t)
                    t += 5 if before else schedule.wait_seconds(t)
                    continue
                request(11 if before else 8, commits=3 if before else 1, redis=2)
                schedule.found()
                request(5 if before else 4)  # Agent GET: read transaction ends in rollback.
                for renewal in range(7):
                    request(5 if before else 4, redis=int(before or renewal < 5))
                    counts["lease_heartbeats"] += 1
                request(13 if before else 12, redis=2)
                counts["results"] += 1
                counts["artifact_writes"] += 6
                counts["executions"] += 1
                completed += 1
                t += 60
                schedule.heartbeat_sent(t)
            request(4 if before else 3, redis=1)  # Graceful daily shutdown.
    counts["producer_http"] = counts["executions"]
    counts["rollbacks"] = counts["executions"]  # Agent download read transaction.
    counts["commits"] = counts["transactions"] - counts["rollbacks"]
    counts["producer_sql"] = 4 * counts["executions"]
    counts["producer_redis"] = 2 * counts["executions"]
    counts["redis_with_producer"] = counts["redis"] + counts["producer_redis"]
    return dict(counts)


if __name__ == "__main__":
    print(
        json.dumps(
            {
                "kind": "virtual-time fixture, NOT actual Mac or cloud measurement",
                "one_worker_idle": simulate(workers=1),
                "before": {str(rate): simulate(rate, before=True) for rate in (0, 1, 5, 10)},
                "after": {str(rate): simulate(rate) for rate in (0, 1, 5, 10)},
            },
            indent=2,
        )
    )
