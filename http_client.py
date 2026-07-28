"""Shared outbound HTTP session configuration for MLB Analytics Hub."""
from __future__ import annotations

import os

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_GLOBAL_SESSION: requests.Session | None = None


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def build_retry_policy() -> Retry:
    retries = max(0, _env_int("HTTP_RETRY_TOTAL", 3))
    backoff = max(0.0, _env_float("HTTP_RETRY_BACKOFF", 0.5))
    return Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"HEAD", "GET", "OPTIONS"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )


def build_http_session() -> requests.Session:
    retry = build_retry_policy()
    session = requests.Session()
    session.mount(
        "https://",
        HTTPAdapter(
            max_retries=retry,
            pool_connections=max(1, _env_int("HTTP_POOL_CONNECTIONS", 16)),
            pool_maxsize=max(1, _env_int("HTTP_POOL_MAXSIZE", 32)),
        ),
    )
    session.mount(
        "http://",
        HTTPAdapter(
            max_retries=retry,
            pool_connections=max(1, _env_int("HTTP_POOL_CONNECTIONS_HTTP", 8)),
            pool_maxsize=max(1, _env_int("HTTP_POOL_MAXSIZE_HTTP", 16)),
        ),
    )
    return session


def install_global_http_session() -> requests.Session:
    """Route module-level requests calls through one pooled retrying session."""
    global _GLOBAL_SESSION
    if _GLOBAL_SESSION is None:
        _GLOBAL_SESSION = build_http_session()
        requests.get = _GLOBAL_SESSION.get
        requests.head = _GLOBAL_SESSION.head
        requests.options = _GLOBAL_SESSION.options
    return _GLOBAL_SESSION
