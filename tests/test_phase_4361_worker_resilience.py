import importlib
import os
import threading
from pathlib import Path
from unittest.mock import patch

import process_manager
from task_queue import JobQueueUnavailable


ROOT = Path(__file__).resolve().parents[1]


class FakeProcess:
    def __init__(self, poll_values):
        self.poll_values = list(poll_values)
        self.terminated = False
        self.killed = False

    def poll(self):
        if len(self.poll_values) > 1:
            return self.poll_values.pop(0)
        return self.poll_values[0]

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        del timeout
        return 0

    def kill(self):
        self.killed = True


def test_worker_exit_restarts_worker_without_terminating_web():
    web = FakeProcess([None, None, 0])
    first_worker = FakeProcess([7])
    replacement_worker = FakeProcess([None])
    worker_starts = iter([first_worker, replacement_worker])
    clock = iter(float(value) for value in range(0, 40, 2))

    with (
        patch.object(process_manager, '_start_web', return_value=web),
        patch.object(process_manager, '_start_worker', side_effect=lambda: next(worker_starts)),
        patch.object(process_manager.signal, 'signal'),
        patch.object(process_manager.time, 'sleep'),
        patch.object(process_manager.time, 'monotonic', side_effect=lambda: next(clock)),
    ):
        assert process_manager.main() == 1

    assert web.terminated is False
    assert first_worker.terminated is False
    assert replacement_worker.terminated is True


def test_worker_waits_for_redis_instead_of_exiting():
    previous_role = os.environ.get('PROCESS_ROLE')
    worker = importlib.import_module('worker')
    if previous_role is None:
        os.environ.pop('PROCESS_ROLE', None)
    else:
        os.environ['PROCESS_ROLE'] = previous_role

    attempts = []
    expected_queue = object()

    def connect():
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise JobQueueUnavailable('temporary outage')
        return expected_queue

    queue = worker._wait_for_queue(
        connect,
        stop_event=threading.Event(),
        initial_backoff=0.01,
        maximum_backoff=0.01,
    )
    assert queue is expected_queue
    assert attempts == [1, 2, 3]


def test_worker_retries_redis_and_deploy_rolls_back_failed_deploys():
    worker = (ROOT / 'worker.py').read_text(encoding='utf-8')
    manager = (ROOT / 'process_manager.py').read_text(encoding='utf-8')
    workflow = (ROOT / '.github/workflows/deploy.yml').read_text(encoding='utf-8')

    assert 'Redis queue unavailable; retrying' in worker
    assert 'reset_job_queue()' in worker
    assert 'reconnecting without stopping web' in worker
    assert 'web remains live and worker restarts' in manager
    assert 'Roll back failed deployment or production smoke' in workflow
    assert "steps.deploy.outcome == 'success'" not in workflow
    assert "steps.deploy.outcome != 'skipped'" in workflow
