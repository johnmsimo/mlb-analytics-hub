import importlib
import os
import threading


def test_pipeline_scheduler_starts_once(monkeypatch):
    scheduler = importlib.import_module("pipeline_scheduler")
    monkeypatch.setattr(scheduler, "_scheduler_started", False)
    started = []

    class DummyThread:
        def __init__(self, *args, **kwargs):
            started.append(kwargs.get("name"))

        def start(self):
            return None

    monkeypatch.setattr(threading, "Thread", DummyThread)
    monkeypatch.setattr(os.path, "exists", lambda _path: False)

    assert scheduler.start_scheduler() is True
    assert scheduler.start_scheduler() is False
    assert started.count("pipeline_scheduler") == 1


def test_bq_scheduler_starts_once(monkeypatch):
    bq_etl = importlib.import_module("bq_etl")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setattr(bq_etl, "_bq_scheduler_started", False)
    started = []

    class DummyThread:
        def __init__(self, *args, **kwargs):
            started.append(kwargs.get("name"))

        def start(self):
            return None

    monkeypatch.setattr(bq_etl._threading, "Thread", DummyThread)

    assert bq_etl.start_bq_scheduler() is True
    assert bq_etl.start_bq_scheduler() is False
    assert started == ["bq_etl_scheduler"]
