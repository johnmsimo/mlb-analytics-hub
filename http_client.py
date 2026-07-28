"""Shared outbound HTTP session configuration for MLB Analytics Hub."""
from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_GLOBAL_SESSION: requests.Session | None = None



from config import settings

def build_retry_policy() -> Retry:
    retries = settings.http_retry_total
    backoff = settings.http_retry_backoff
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
            pool_connections=settings.http_pool_connections,
            pool_maxsize=settings.http_pool_maxsize,
        ),
    )
    session.mount(
        "http://",
        HTTPAdapter(
            max_retries=retry,
            pool_connections=settings.http_pool_connections_http,
            pool_maxsize=settings.http_pool_maxsize_http,
        ),
    )
    return session


def get_http_session() -> requests.Session:
    """Return the process-wide pooled retrying session without monkey-patching."""
    global _GLOBAL_SESSION
    if _GLOBAL_SESSION is None:
        _GLOBAL_SESSION = build_http_session()
    return _GLOBAL_SESSION


def install_global_http_session() -> requests.Session:
    """Route module-level requests calls through one pooled retrying session."""
    session = get_http_session()
    requests.get = session.get
    requests.head = session.head
    requests.options = session.options
    return session
