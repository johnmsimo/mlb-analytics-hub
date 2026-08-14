import time

from task_queue import HEARTBEAT_KEY, QUEUE_KEY, RedisJobQueue


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.queues = {}
        self.setex_calls = {}
        self.expired = {}

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
        self.setex_calls[key] = self.setex_calls.get(key, 0) + 1
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)

    def expire(self, key, ttl):
        self.expired[key] = ttl
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


def test_deduped_job_fails_closed_after_completion_window():
    redis = FakeRedis()
    queue = RedisJobQueue(redis)
    job = queue.enqueue(
        'props_scan',
        {'date': '2026-08-14'},
        dedupe_key='props-scan:2026-08-14',
        timeout_seconds=30,
    )
    job['queuedAt'] = time.time() - 31
    queue._save(job)

    stale = queue.get_deduped('props-scan:2026-08-14')
    snapshot = queue.snapshot(stale)

    assert stale['status'] == 'error'
    assert stale['finishedAt'] is not None
    assert 'bounded completion window' in stale['error']
    assert snapshot['status'] == 'error'
    assert snapshot['timeoutSeconds'] == 30
    assert snapshot['maxAttempts'] == 2
    assert redis.expired[queue._dedupe_key('props-scan:2026-08-14')] == 30


def test_queue_health_requires_recent_worker_heartbeat():
    redis = FakeRedis()
    queue = RedisJobQueue(redis)
    assert queue.health()['workerReady'] is False
    redis.values[HEARTBEAT_KEY] = str(time.time())
    health = queue.health()
    assert health['connected'] is True
    assert health['workerReady'] is True


def test_long_job_refreshes_worker_heartbeat_until_completion():
    redis = FakeRedis()
    queue = RedisJobQueue(redis, heartbeat_interval_seconds=0.01)
    job = queue.enqueue('simulation', {'gamePk': 11}, dedupe_key='sim:11')

    def slow_job(_args):
        time.sleep(0.05)

    assert queue.work_once({'simulation': slow_job})
    assert queue.get(job['id'])['status'] == 'done'
    # One heartbeat is written before the handler, at least one while it is
    # running, and another after completion.
    assert redis.setex_calls.get(HEARTBEAT_KEY, 0) >= 3


def test_redis_socket_timeout_exceeds_block_timeout():
    from task_queue import _redis_socket_timeout

    assert _redis_socket_timeout(5) > 5
    assert _redis_socket_timeout(30) > 30
