import json
import logging

from redis import Redis, RedisError

from .models import now

log = logging.getLogger(__name__)


class RedisQueue:
    """Recoverable notification index. PostgreSQL is authoritative, including on Redis loss."""

    def __init__(self, url: str):
        self.redis = Redis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)

    def enqueue(self, job_id: str):
        try:
            self.redis.zadd("abx:queued", {job_id: now().timestamp()})
        except RedisError:
            log.warning("Redis queue unavailable; durable database polling remains active")

    def claimed(self, job_id: str):
        try:
            self.redis.zrem("abx:queued", job_id)
        except RedisError:
            log.warning("Redis claim notification unavailable")

    def publish(self, event_type: str, **data):
        event = {"type": event_type, "timestamp": now().isoformat(), **data}
        try:
            self.redis.publish("abx:events", json.dumps(event))
        except RedisError:
            log.warning("Redis event unavailable; clients reconcile using REST")
        return event
