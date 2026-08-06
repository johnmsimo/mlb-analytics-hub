import time

from task_queue import HEARTBEAT_KEY, QUEUE_KEY, RedisJobQueue


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.queues = {}

    def ping(self):
        return True

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, nx=False, ex=None):
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def setex(self, key, ttl, value):
        del ttl
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)

    def expire(self, key, ttl):
        del key, ttl
        return True

    def rpush(self, key, value):
        self.queues.setdefault(key, []).append(value)

    def blpop(self, key, timeout=0):
        del timeout
        queue = self.queues.setdefault(key, [])
        return (key, queue.pop(0)) if queue else None

    def llen(self, key):
        return len(self.queues.get(key, []))


def test_queue_deduplicates_and_worker_completes_job():
    redis = FakeRedis()
    queue = RedisJobQueue(redis)
    first = queue.enqueue('simulation', {'gamePk': 7}, dedupe_key='sim:7')
    duplicate = queue.enqueue('simulation', {'gamePk': 7}, dedupe_key='sim:7')

    assert duplicate['id'] == first['id']
    assert redis.llen(QUEUE_KEY) == 1

    calls = []
    assert queue.work_once({'simulation': lambda args: calls.append(args['gamePk'])})
    finished = queue.get(first['id'])
    assert calls == [7]
    assert finished['status'] == 'done'
    assert redis.get(HEARTBEAT_KEY) is not None


def test_queue_retries_once_then_reports_error():
    redis = FakeRedis()
    queue = RedisJobQueue(redis)
    job = queue.enqueue(
        'simulation', {'gamePk': 9}, dedupe_key='sim:9', max_attempts=2
    )

    def fail(_args):
        raise RuntimeError('boom')

    assert queue.work_once({'simulation': fail})
    assert queue.get(job['id'])['status'] == 'queued'
    assert queue.work_once({'simulation': fail})
    failed = queue.get(job['id'])
    assert failed['status'] == 'error'
    assert failed['error'] == 'Background job failed. Retry or check server logs.'


def test_queue_health_requires_recent_worker_heartbeat():
    redis = FakeRedis()
    queue = RedisJobQueue(redis)
    assert queue.health()['workerReady'] is False
    redis.values[HEARTBEAT_KEY] = str(time.time())
    health = queue.health()
    assert health['connected'] is True
    assert health['workerReady'] is True
