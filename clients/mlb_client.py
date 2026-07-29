"""Pooled, retrying client for the MLB Stats API."""
from __future__ import annotations

import logging
import re
from time import perf_counter
from typing import Any, Mapping

import requests

from config import settings
from http_client import get_http_session

log = logging.getLogger(__name__)
_NUMERIC_PATH_SEGMENT = re.compile(r"/\d+(?=/|$)")


class MLBClient:
    """Small response-contract-neutral wrapper around MLB Stats API GETs."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        base_url: str | None = None,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/") if base_url else None

    @property
    def base_url(self) -> str:
        return self._base_url or settings.mlb_stats_api_base_url

    def _endpoint_label(self, api_version: str, path: str) -> str:
        normalized = _NUMERIC_PATH_SEGMENT.sub("/:id", f"/{path.lstrip('/')}")
        return f"/{api_version.strip('/')}{normalized}"

    def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        timeout: int | float | None = None,
        api_version: str = "v1",
    ) -> dict[str, Any]:
        """GET one MLB endpoint and return its JSON object."""
        clean_path = path.lstrip("/")
        clean_version = api_version.strip("/")
        url = f"{self.base_url}/{clean_version}/{clean_path}"
        request_timeout = timeout or settings.mlb_http_timeout
        endpoint = self._endpoint_label(clean_version, clean_path)
        started = perf_counter()
        status_code: int | None = None

        try:
            session = self._session or get_http_session()
            response = session.get(
                url,
                params=dict(params) if params is not None else None,
                timeout=request_timeout,
            )
            status_code = response.status_code
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError(f"MLB API returned non-object JSON for {endpoint}")
            return payload
        except Exception:
            log.warning(
                "mlb_upstream_error endpoint=%s status=%s",
                endpoint,
                status_code,
                exc_info=True,
            )
            raise
        finally:
            elapsed_ms = (perf_counter() - started) * 1000
            if elapsed_ms >= settings.mlb_slow_request_ms:
                log.warning(
                    "mlb_upstream_slow endpoint=%s status=%s duration_ms=%.1f",
                    endpoint,
                    status_code,
                    elapsed_ms,
                )

    def schedule(
        self,
        *,
        date_str: str | None = None,
        game_pk: Any = None,
        hydrate: str | None = None,
        timeout: int | float | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"sportId": 1}
        if date_str is not None:
            params["date"] = str(date_str)
        if game_pk is not None:
            params["gamePk"] = game_pk
        if hydrate:
            params["hydrate"] = hydrate

        payload = self.get_json("schedule", params=params, timeout=timeout)
        games = [
            game
            for date_block in payload.get("dates", [])
            if isinstance(date_block, dict)
            for game in date_block.get("games", [])
            if isinstance(game, dict)
        ]
        return games

    def person(
        self,
        person_id: Any,
        *,
        hydrate: str | None = None,
        timeout: int | float | None = None,
    ) -> dict[str, Any]:
        params = {"hydrate": hydrate} if hydrate else None
        return self.get_json(
            f"people/{person_id}",
            params=params,
            timeout=timeout,
        )

    def person_stats(
        self,
        person_id: Any,
        *,
        timeout: int | float | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        return self.get_json(
            f"people/{person_id}/stats",
            params=params,
            timeout=timeout,
        )

    def team_roster(
        self,
        team_id: Any,
        *,
        roster_type: str = "active",
        timeout: int | float | None = None,
    ) -> dict[str, Any]:
        return self.get_json(
            f"teams/{team_id}/roster",
            params={"rosterType": roster_type},
            timeout=timeout,
        )

    def stats(
        self,
        *,
        timeout: int | float | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        return self.get_json("stats", params=params, timeout=timeout)

    def game_boxscore(
        self,
        game_pk: Any,
        *,
        timeout: int | float | None = None,
    ) -> dict[str, Any]:
        return self.get_json(
            f"game/{game_pk}/boxscore",
            timeout=timeout,
        )

    def game_live_feed(
        self,
        game_pk: Any,
        *,
        timeout: int | float | None = None,
    ) -> dict[str, Any]:
        return self.get_json(
            f"game/{game_pk}/feed/live",
            timeout=timeout,
            api_version="v1.1",
        )


mlb_client = MLBClient()
