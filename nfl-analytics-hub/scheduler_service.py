#!/usr/bin/env python3
import signal
import time
from app import app
from scheduled_jobs import start_scheduler

scheduler = start_scheduler(app)
if scheduler is None:
    raise SystemExit("Set ENABLE_SCHEDULER=true")
stop = False
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__('stop', True))
signal.signal(signal.SIGINT, lambda *_: globals().__setitem__('stop', True))
while not stop:
    time.sleep(1)
scheduler.shutdown(wait=False)
