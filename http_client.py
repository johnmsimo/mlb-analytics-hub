"""Shared outbound HTTP session configuration for MLB Analytics Hub."""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import requests
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict
from urllib3.util.retry import Retry

from cache_service import get_or_compute, normalize_cache_key
from config import settings

_GLOBAL_SESSION: requests.Session | None = None
_MLB_BOXSCORE_PATH = re.compile(r"/v1/game/(\d+)/boxscore/?$")
_MLB_LIVE_FEED_PATH = re.compile(r"/v1\.1/game/(\d+)/feed/live/?$")
_MLB_SCHEDULE_PATH = re.compile(r"/v1/schedule/?$")
_MLB_REFERENCE_PATHS = (
    ("mlb_person_stats", re.compile(r"/v1/people/(\d+)/stats/?$"), "stats"),
    ("mlb_person", re.compile(r"/v1/people/(\d+)/?$"), "stats"),
    ("mlb_people", re.compile(r"/v1/people/?$"), "stats"),
    ("mlb_team_roster", re.compile(r"/v1/teams/(\d+)/roster/?$"), "schedule"),
    ("mlb_team_venue", re.compile(r"/v1/teams/(\d+)/venue/?$"), "static"),
    ("mlb_team_stats", re.compile(r"/v1/teams/(\d+)/stats/?$"), "stats"),
    ("mlb_team", re.compile(r"/v1/teams/(\d+)/?$"), "stats"),
    ("mlb_teams", re.compile(r"/v1/teams/?$"), "static"),
    ("mlb_sport_players", re.compile(r"/v1/sports/(\d+)/players/?$"), "stats"),
    ("mlb_stats", re.compile(r"/v1/stats(?:/leaders)?/?$"), "stats"),
    ("mlb_standings", re.compile(r"/v1/standings/?$"), "schedule"),
    ("mlb_transactions", re.compile(r"/v1/transactions/?$"), "schedule"),
)


@dataclass(frozen=True)
class _MLBCacheTarget:
    namespace: str
    identity: tuple[Any, ...]
    policy: str
    ttl_seconds: int | None = None


def _mlb_cache_target(url: str) -> _MLBCacheTarget | None:
    parsed = urlsplit(str(url))
    configured_host = urlsplit(settings.mlb_stats_api_base_url).netloc
    if parsed.netloc not in {"statsapi.mlb.com", configured_host}:
        return None
    normalized_query = tuple(sorted(parse_qsl(parsed.query, keep_blank_values=True)))

    for namespace, pattern in (
        ("mlb_boxscore", _MLB_BOXSCORE_PATH),
        ("mlb_live_feed", _MLB_LIVE_FEED_PATH),
    ):
        match = pattern.search(parsed.path)
        if match:
            identity: tuple[Any, ...] = (int(match.group(1)),)
            if normalized_query:
                identity = (*identity, normalized_query)
            return _MLBCacheTarget(namespace, identity, "live")

    if _MLB_SCHEDULE_PATH.search(parsed.path):
        return _MLBCacheTarget(
            "mlb_schedule",
            (parsed.path.rstrip("/"), normalized_query),
            "schedule",
            settings.mlb_schedule_cache_ttl,
        )

    for namespace, pattern, policy in _MLB_REFERENCE_PATHS:
        match = pattern.search(parsed.path)
        if match:
            identity = tuple(match.groups()) or (parsed.path.rstrip("/"),)
            return _MLBCacheTarget(
                namespace,
                (*identity, normalized_query),
                policy,
            )
    return None


def _response_snapshot(response: requests.Response) -> dict[str, Any]:
    return {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "content": base64.b64encode(response.content).decode("ascii"),
        "url": response.url,
        "reason": response.reason,
        "encoding": response.encoding,
    }


def _response_from_snapshot(snapshot: dict[str, Any]) -> requests.Response:
    response = requests.Response()
    response.status_code = int(snapshot["status_code"])
    response.headers = CaseInsensitiveDict(snapshot.get("headers") or {})
    response._content = base64.b64decode(snapshot.get("content") or "")
    response.url = str(snapshot.get("url") or "")
    response.reason = snapshot.get("reason")
    response.encoding = snapshot.get("encoding")
    return response


class _SharedSession(requests.Session):
    """Retrying session with resilient caching for repeated MLB read payloads."""

    def get(self, url, **kwargs):
        target = _mlb_cache_target(str(url))
        if target is None or kwargs.get("stream"):
            return super().get(url, **kwargs)

        key = normalize_cache_key(
            target.namespace,
            *target.identity,
            params=kwargs.get("params"),
        )

        def fetch() -> dict[str, Any]:
            response = super(_SharedSession, self).get(url, **kwargs)
            response.raise_for_status()
            return _response_snapshot(response)

        try:
            snapshot = get_or_compute(
                key,
                fetch,
                ttl=target.ttl_seconds,
                policy=target.policy,
                allow_stale=True,
            )
        except requests.HTTPError as exc:
            if exc.response is not None:
                return exc.response
            raise
        return _response_from_snapshot(snapshot)


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
    session = _SharedSession()
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
