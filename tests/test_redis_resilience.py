import unittest

from redis_client import _ResilientClient


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.fail = False

    def _check(self):
        if self.fail:
            raise ConnectionError("redis unavailable")

    def ping(self):
        self._check()
        return True

    def get(self, key):
        self._check()
        return self.values.get(key)

    def ttl(self, key):
        self._check()
        return 60

    def set(self, key, value, ttl=None):
        self._check()
        self.values[key] = value

    def delete(self, key):
        self._check()
        self.values.pop(key, None)


class RedisResilienceTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeRedis()
        self.client = _ResilientClient(
            "redis://example:6379/0",
            health_interval=30,
            failure_threshold=2,
            circuit_timeout=60,
            redis_factory=lambda _url: self.transport,
            start_monitor=False,
        )

    def test_writes_are_available_during_redis_outage(self):
        self.client.set("lineup:13", {"ready": True}, ttl=60)
        self.transport.fail = True

        self.assertEqual(self.client.get("lineup:13"), {"ready": True})
        self.assertEqual(self.client.get("lineup:13"), {"ready": True})
        status = self.client.status()
        self.assertEqual(status["circuit_state"], "open")
        self.assertEqual(status["backend"], "memory")
        self.assertTrue(status["fallback_active"])

    def test_health_probe_recovers_and_closes_circuit(self):
        self.client.set("stats:13", {"hits": 2}, ttl=60)
        self.transport.fail = True
        self.client.get("stats:13")
        self.client.get("stats:13")
        self.assertEqual(self.client.status()["circuit_state"], "open")

        self.transport.fail = False
        self.assertTrue(self.client.check_health(force=True))
        status = self.client.status()
        self.assertEqual(status["circuit_state"], "closed")
        self.assertEqual(status["backend"], "redis")
        self.assertEqual(status["consecutive_failures"], 0)
        self.assertIsNotNone(status["last_success_at"])

    def test_unconfigured_client_reports_memory_without_failure(self):
        client = _ResilientClient(
            "",
            health_interval=30,
            failure_threshold=5,
            circuit_timeout=60,
            start_monitor=False,
        )
        client.set("local", 13, ttl=60)
        self.assertEqual(client.get("local"), 13)
        self.assertEqual(client.status()["circuit_state"], "disabled")
        self.assertFalse(client.status()["configured"])


if __name__ == "__main__":
    unittest.main()
