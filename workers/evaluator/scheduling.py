"""Independent idle deadlines; no network, threads or wall-clock dependencies."""

import random


class IdleSchedule:
    def __init__(self, settings, rng=None):
        self.rng = rng or random.Random()
        self.minimum = settings.worker_idle_poll_min_seconds
        self.maximum = settings.worker_idle_poll_max_seconds
        self.heartbeat_interval = settings.worker_idle_heartbeat_seconds
        self.delay = self.minimum
        self.next_poll = self.next_heartbeat = 0.0

    def startup_delay(self):
        return self.rng.uniform(0, min(self.minimum, 5))

    def heartbeat_due(self, now):
        return now >= self.next_heartbeat

    def poll_due(self, now):
        return now >= self.next_poll

    def heartbeat_sent(self, now):
        self.next_heartbeat = now + self.heartbeat_interval * self.rng.uniform(0.9, 1)

    def empty(self, now):
        # Never exceed the configured maximum latency or busy-spin with full jitter.
        self.next_poll = now + self.rng.uniform(max(self.minimum, self.delay * 0.8), self.delay)
        self.delay = min(self.maximum, self.delay * 2)

    def found(self):
        self.delay = self.minimum
        self.next_poll = 0.0

    def wait_seconds(self, now):
        return max(0, min(self.next_poll, self.next_heartbeat) - now)
