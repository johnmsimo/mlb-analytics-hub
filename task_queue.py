"""Redis-backed durable jobs shared by the web and worker process.

The queue deliberately has no in-memory production fallback.  A process-local
queue would acknowledge work that a separate worker can never see, recreating
the request-thread failure mode this module replaces.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from config import settings


log = logging.getLogger(__name__)
QUEUE_KEY = "mlb:jobs:queue:v1"
HEARTBEAT_KEY = "mlb:jobs:worker-heartbeat:v1"
JOB_PREFIX = "mlb:jobs:data:v1:"
DEDUPE_PREFIX = "mlb:jobs:dedupe:v1:"
STALE_JOB_ERROR = "Background job exceeded its bounded completion window."
_DELETE_DEDUPE_IF_OWNER = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""
_EXPIRE_DEDUPE_IF_OWNER = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], ARGV[2])
end
return 0
"""


class JobQueueUnavailable(RuntimeError):
    """Raised when durable jobs cannot be accepted safely."""


def _redis_socket_timeout(block_seconds: int) -> float:
    """Keep Redis reads alive longer than the worker's blocking BLPOP."""
    return max(2.0, float(block_seconds) + 2.0)


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


@dataclass
class RedisJobQueue:
    client: Any
    job_ttl: int = 3600
    heartbeat_interval_seconds: float = 20.0

    def _job_key(self, job_id: str) -> str:
        return f"{JOB_PREFIX}{job_id}"

    def _dedupe_key(self, dedupe_key: str) -> str:
        return f"{DEDUPE_PREFIX}{dedupe_key}"

    def _delete_dedupe_if_owner(self, dedupe: str, job_id: str) -> bool:
        """Release a dedupe lease only while it still belongs to this job."""
        return bool(self.client.eval(_DELETE_DEDUPE_IF_OWNER, 1, dedupe, job_id))

    def _expire_dedupe_if_owner(
        self,
        dedupe: str,
        job_id: str,
        ttl: int,
    ) -> bool:
        """Shorten a failed job lease without touching a replacement lease."""
        return bool(
            self.client.eval(
                _EXPIRE_DEDUPE_IF_OWNER,
                1,
                dedupe,
                job_id,
                max(1, int(ttl)),
            )
        )

    def _save(self, job: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(job)
        self.client.setex(self._job_key(value["id"]), self.job_ttl, _json(value))
        return value

    def get(self, job_id: str | None) -> dict[str, Any] | None:
        if not job_id:
            return None
        raw = self.client.get(self._job_key(job_id))
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def get_deduped(
        self,
        dedupe_key: str,
        *,
        fail_stale: bool = True,
    ) -> dict[str, Any] | None:
        """Return the current deduplicated job and fail closed when it is stale."""
        dedupe = self._dedupe_key(dedupe_key)
        existing_id = self.client.get(dedupe)
        existing = self.get(str(existing_id)) if existing_id else None
        if not existing:
            return None
        status = str(existing.get("status") or "")
        started = float(existing.get("startedAt") or existing.get("queuedAt") or 0)
        timeout = max(30, int(existing.get("timeoutSeconds") or 300))
        timed_out = (
            fail_stale
            and status in {"queued", "running"}
            and started
            and time.time() - started > timeout
        )
        if timed_out:
            existing.update({
                "status": "error",
                "finishedAt": time.time(),
                "error": STALE_JOB_ERROR,
            })
            self._save(existing)

        # A timed-out lease is terminal, but retaining it briefly makes every
        # request fail with the same stale job. Release only this job's lease
        # so the caller can enqueue a fresh bounded attempt immediately. The
        # ownership check prevents a late worker from deleting a replacement.
        if timed_out or (
            fail_stale
            and status == "error"
            and existing.get("error") == STALE_JOB_ERROR
        ):
            if self._delete_dedupe_if_owner(dedupe, str(existing["id"])):
                return None
            replacement_id = self.client.get(dedupe)
            return self.get(str(replacement_id)) if replacement_id else None
        return existing

    def enqueue(
        self,
        kind: str,
        args: Mapping[str, Any],
        *,
        dedupe_key: str,
        timeout_seconds: int = 300,
        max_attempts: int = 2,
    ) -> dict[str, Any]:
        dedupe = self._dedupe_key(dedupe_key)
        existing = self.get_deduped(dedupe_key)
        if existing:
            if existing.get("status") in {"queued", "running"}:
                return existing
            self.client.delete(dedupe)

        job_id = uuid.uuid4().hex
        if not self.client.set(dedupe, job_id, nx=True, ex=max(timeout_seconds * 2, 60)):
            return self.get(self.client.get(dedupe)) or {
                "id": job_id,
                "kind": kind,
                "status": "queued",
                "queuedAt": time.time(),
            }
        job = {
            "id": job_id,
            "kind": kind,
            "args": dict(args),
            "dedupeKey": dedupe_key,
            "status": "queued",
            "queuedAt": time.time(),
            "startedAt": None,
            "finishedAt": None,
            "attempt": 0,
            "maxAttempts": max(1, int(max_attempts)),
            "timeoutSeconds": max(30, int(timeout_seconds)),
            "error": None,
        }
        self._save(job)
        self.client.rpush(QUEUE_KEY, job_id)
        return job

    def snapshot(self, job: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not job:
            return None
        started = float(job.get("startedAt") or job.get("queuedAt") or time.time())
        return {
            "id": job.get("id"),
            "status": job.get("status") or "queued",
            "elapsedSeconds": max(0, int(time.time() - started)),
            "attempt": int(job.get("attempt") or 0),
            "maxAttempts": int(job.get("maxAttempts") or 1),
            "timeoutSeconds": int(job.get("timeoutSeconds") or 0),
            "error": job.get("error"),
        }

    def heartbeat(self) -> None:
        self.client.setex(HEARTBEAT_KEY, 90, str(time.time()))

    def health(self) -> dict[str, Any]:
        self.client.ping()
        raw = self.client.get(HEARTBEAT_KEY)
        try:
            heartbeat_age = max(0.0, time.time() - float(raw)) if raw else None
        except (TypeError, ValueError):
            heartbeat_age = None
        return {
            "connected": True,
            "queued": int(self.client.llen(QUEUE_KEY) or 0),
            "workerHeartbeatAgeSeconds": (
                round(heartbeat_age, 2) if heartbeat_age is not None else None
            ),
            "workerReady": heartbeat_age is not None and heartbeat_age < 60,
        }

    def work_once(
        self,
        handlers: Mapping[str, Callable[[Mapping[str, Any]], Any]],
        *,
        block_seconds: int = 5,
    ) -> bool:
        self.heartbeat()
        claimed = self.client.blpop(QUEUE_KEY, timeout=max(1, int(block_seconds)))
        if not claimed:
            return False
        job_id = claimed[1] if isinstance(claimed, (list, tuple)) else claimed
        job = self.get(str(job_id))
        if not job or job.get("status") != "queued":
            return False

        handler = handlers.get(str(job.get("kind") or ""))
        if handler is None:
            self._finish(job, error=f"Unknown job kind: {job.get('kind')}")
            return True

        job.update({
            "status": "running",
            "startedAt": time.time(),
            "attempt": int(job.get("attempt") or 0) + 1,
            "error": None,
        })
        self._save(job)
        self.heartbeat()
        heartbeat_stop = threading.Event()

        def keep_worker_ready() -> None:
            while not heartbeat_stop.wait(max(0.01, self.heartbeat_interval_seconds)):
                try:
                    self.heartbeat()
                except Exception:
                    # The main worker loop owns reconnection.  A heartbeat
                    # failure must not interrupt an otherwise recoverable job.
                    log.warning(
                        "worker heartbeat failed during job kind=%s id=%s",
                        job.get("kind"),
                        job.get("id"),
                        exc_info=True,
                    )

        heartbeat_thread = threading.Thread(
            target=keep_worker_ready,
            name=f"job-heartbeat-{job['id'][:8]}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            handler(dict(job.get("args") or {}))
        except Exception as exc:
            log.exception("durable job failed kind=%s id=%s", job.get("kind"), job["id"])
            if int(job["attempt"]) < int(job.get("maxAttempts") or 1):
                job.update({
                    "status": "queued",
                    "startedAt": None,
                    "error": "Background job failed and was queued for retry.",
                })
                self._save(job)
                self.client.rpush(QUEUE_KEY, job["id"])
            else:
                self._finish(
                    job,
                    error="Background job failed. Retry or check server logs.",
                )
        else:
            self._finish(job)
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1)
        self.heartbeat()
        return True

    def _finish(self, job: dict[str, Any], error: str | None = None) -> None:
        job.update({
            "status": "error" if error else "done",
            "finishedAt": time.time(),
            "error": error,
        })
        self._save(job)
        dedupe = self._dedupe_key(str(job.get("dedupeKey") or ""))
        job_id = str(job.get("id") or "")
        if error:
            self._expire_dedupe_if_owner(dedupe, job_id, 30)
        else:
            self._delete_dedupe_if_owner(dedupe, job_id)


_queue: RedisJobQueue | None = None


def reset_job_queue() -> None:
    """Drop a failed cached client so the worker can establish a new connection."""
    global _queue
    _queue = None


def get_job_queue() -> RedisJobQueue:
    global _queue
    if _queue is not None:
        return _queue
    if not settings.redis_url:
        raise JobQueueUnavailable("REDIS_URL is required for durable background jobs.")
    try:
        import redis

        client = redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=0.5,
            # BLPOP may legitimately wait for the configured idle period.
            # A shorter socket timeout turns an empty queue into a false
            # connection failure and causes needless worker reconnect churn.
            socket_timeout=_redis_socket_timeout(settings.redis_queue_block_seconds),
            health_check_interval=30,
        )
        client.ping()
    except Exception as exc:
        raise JobQueueUnavailable("Redis job queue is unavailable.") from exc
    _queue = RedisJobQueue(client, job_ttl=settings.job_result_ttl)
    return _queue


def enqueue_job(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return get_job_queue().enqueue(*args, **kwargs)


def get_deduped_job(dedupe_key: str) -> dict[str, Any] | None:
    queue = get_job_queue()
    return queue.snapshot(queue.get_deduped(dedupe_key))


def write_durable_json(key: str, value: Any, *, ttl: int) -> None:
    """Write worker output directly to Redis and verify it is externally visible."""
    queue = get_job_queue()
    queue.client.setex(key, max(1, int(ttl)), _json(value))
    if queue.client.get(key) is None:
        raise JobQueueUnavailable("Redis did not persist the worker snapshot.")


def queue_health() -> dict[str, Any]:
    if not settings.redis_url:
        return {
            "connected": False,
            "workerReady": False,
            "error": "redis_not_configured",
        }
    try:
        return get_job_queue().health()
    except JobQueueUnavailable:
        return {
            "connected": False,
            "workerReady": False,
            "error": "redis_unavailable",
        }
