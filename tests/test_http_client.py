import os
import unittest
from unittest.mock import patch

import requests

import cache_service
import http_client
from http_client import build_http_session, build_retry_policy, get_http_session
from redis_client import _MemoryClient


def _response(url, payload=b'{"teams": {}}', status=200):
    response = requests.Response()
    response.status_code = status
    response._content = payload
    response.url = url
    response.headers["Content-Type"] = "application/json"
    return response


class HttpClientTests(unittest.TestCase):
    def setUp(self):
        self.cache = _MemoryClient()
        cache_service._locks.clear()
        cache_service.reset_cache_metrics()
        self.cache_patch = patch("cache_service.get_redis", return_value=self.cache)
        self.cache_patch.start()
        self.requests_get = requests.get
        self.requests_head = requests.head
        self.requests_options = requests.options

    def tearDown(self):
        http_client._GLOBAL_SESSION = None
        requests.get = self.requests_get
        requests.head = self.requests_head
        requests.options = self.requests_options
        self.cache_patch.stop()

    def test_retry_policy_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            retry = build_retry_policy()
        self.assertEqual(retry.total, 3)
        self.assertEqual(retry.backoff_factor, 0.5)
        self.assertIn(429, retry.status_forcelist)
        self.assertIn("GET", retry.allowed_methods)
        self.assertNotIn("POST", retry.allowed_methods)

    def test_retry_policy_honors_environment(self):
        with patch.dict(
            os.environ,
            {"HTTP_RETRY_TOTAL": "5", "HTTP_RETRY_BACKOFF": "1.25"},
            clear=True,
        ):
            retry = build_retry_policy()
        self.assertEqual(retry.total, 5)
        self.assertEqual(retry.backoff_factor, 1.25)

    def test_session_mounts_retry_adapters(self):
        with patch.dict(os.environ, {}, clear=True):
            session = build_http_session()
        self.assertEqual(session.get_adapter("https://").max_retries.total, 3)
        self.assertEqual(session.get_adapter("http://").max_retries.total, 3)

    def test_global_install_is_idempotent(self):
        with patch.dict(os.environ, {}, clear=True):
            first = http_client.install_global_http_session()
            second = http_client.install_global_http_session()
        self.assertIs(first, second)
        self.assertEqual(requests.get.__self__, first)

    def test_shared_session_does_not_require_global_install(self):
        with patch.dict(os.environ, {}, clear=True):
            first = get_http_session()
            second = get_http_session()
        self.assertIs(first, second)

    def test_boxscore_requests_are_reused(self):
        url = "https://statsapi.mlb.com/api/v1/game/99113/boxscore"
        response = _response(url)
        session = build_http_session()

        with patch.object(
            requests.Session,
            "get",
            return_value=response,
        ) as upstream:
            first = session.get(url, timeout=10)
            second = session.get(url, timeout=8)

        self.assertEqual(first.json(), {"teams": {}})
        self.assertEqual(second.json(), {"teams": {}})
        upstream.assert_called_once_with(url, timeout=10)

    def test_live_feed_and_boxscore_keys_do_not_collide(self):
        box_url = "https://statsapi.mlb.com/api/v1/game/99113/boxscore"
        live_url = "https://statsapi.mlb.com/api/v1.1/game/99113/feed/live"
        session = build_http_session()

        with patch.object(
            requests.Session,
            "get",
            side_effect=[
                _response(box_url, b'{"kind": "boxscore"}'),
                _response(live_url, b'{"kind": "live"}'),
            ],
        ) as upstream:
            boxscore = session.get(box_url)
            live_feed = session.get(live_url)

        self.assertEqual(boxscore.json()["kind"], "boxscore")
        self.assertEqual(live_feed.json()["kind"], "live")
        self.assertEqual(upstream.call_count, 2)

    def test_upstream_error_serves_stale_game_response(self):
        url = "https://statsapi.mlb.com/api/v1/game/99113/boxscore"
        key = cache_service.normalize_cache_key(
            "mlb_boxscore",
            99113,
            params=None,
        )
        self.cache.set(
            f"{key}:stale",
            http_client._response_snapshot(
                _response(url, b'{"teams": {"away": {"score": 13}}}')
            ),
            ttl=60,
        )
        session = build_http_session()

        with patch.object(
            requests.Session,
            "get",
            side_effect=requests.ConnectionError("MLB unavailable"),
        ):
            response = session.get(url)

        self.assertEqual(response.json()["teams"]["away"]["score"], 13)
        self.assertEqual(cache_service.cache_status()["metrics"]["stale_hits"], 1)

    def test_person_requests_are_reused(self):
        url = "https://statsapi.mlb.com/api/v1/people/13"
        session = build_http_session()

        with patch.object(
            requests.Session,
            "get",
            return_value=_response(url, b'{"people": []}'),
        ) as upstream:
            session.get(url)
            session.get(url)

        upstream.assert_called_once_with(url)

    def test_person_stats_params_isolate_cache_entries(self):
        url = "https://statsapi.mlb.com/api/v1/people/13/stats"
        session = build_http_session()

        with patch.object(
            requests.Session,
            "get",
            side_effect=[
                _response(url, b'{"stats": [{"group": "hitting"}]}'),
                _response(url, b'{"stats": [{"group": "pitching"}]}'),
            ],
        ) as upstream:
            hitting = session.get(url, params={"group": "hitting", "season": 2026})
            hitting_again = session.get(
                url,
                params={"season": 2026, "group": "hitting"},
            )
            pitching = session.get(url, params={"group": "pitching", "season": 2026})

        self.assertEqual(hitting.json(), hitting_again.json())
        self.assertNotEqual(hitting.json(), pitching.json())
        self.assertEqual(upstream.call_count, 2)

    def test_embedded_query_order_is_normalized(self):
        first_url = (
            "https://statsapi.mlb.com/api/v1/standings?"
            "season=2026&leagueId=103"
        )
        reordered_url = (
            "https://statsapi.mlb.com/api/v1/standings?"
            "leagueId=103&season=2026"
        )
        session = build_http_session()

        with patch.object(
            requests.Session,
            "get",
            return_value=_response(first_url, b'{"records": []}'),
        ) as upstream:
            first = session.get(first_url)
            second = session.get(reordered_url)

        self.assertEqual(first.json(), second.json())
        upstream.assert_called_once_with(first_url)

    def test_reference_endpoints_use_endpoint_appropriate_policies(self):
        cases = (
            ("https://statsapi.mlb.com/api/v1/people/13", "stats"),
            ("https://statsapi.mlb.com/api/v1/teams/13/roster", "schedule"),
            ("https://statsapi.mlb.com/api/v1/teams", "static"),
            ("https://statsapi.mlb.com/api/v1/transactions", "schedule"),
        )

        for url, expected_policy in cases:
            with self.subTest(url=url):
                target = http_client._mlb_cache_target(url)
                self.assertIsNotNone(target)
                self.assertEqual(target.policy, expected_policy)

    def test_upstream_error_serves_stale_reference_response(self):
        url = "https://statsapi.mlb.com/api/v1/people/13"
        target = http_client._mlb_cache_target(url)
        self.assertIsNotNone(target)
        key = cache_service.normalize_cache_key(
            target.namespace,
            *target.identity,
            params=None,
        )
        self.cache.set(
            f"{key}:stale",
            http_client._response_snapshot(
                _response(url, b'{"people": [{"id": 13, "fullName": "John"}]}')
            ),
            ttl=60,
        )
        session = build_http_session()

        with patch.object(
            requests.Session,
            "get",
            side_effect=requests.ConnectionError("MLB unavailable"),
        ):
            response = session.get(url)

        self.assertEqual(response.json()["people"][0]["id"], 13)
        self.assertEqual(cache_service.cache_status()["metrics"]["stale_hits"], 1)

    def test_external_and_streaming_requests_bypass_cache(self):
        external_url = "https://example.test/api/v1/people/13"
        mlb_url = "https://statsapi.mlb.com/api/v1/people/13"
        session = build_http_session()

        with patch.object(
            requests.Session,
            "get",
            side_effect=[
                _response(external_url, b'{"people": []}'),
                _response(external_url, b'{"people": []}'),
                _response(mlb_url, b'{"people": []}'),
                _response(mlb_url, b'{"people": []}'),
            ],
        ) as upstream:
            session.get(external_url)
            session.get(external_url)
            session.get(mlb_url, stream=True)
            session.get(mlb_url, stream=True)

        self.assertEqual(upstream.call_count, 4)

    def test_unsuccessful_response_is_not_cached_without_stale_data(self):
        url = "https://statsapi.mlb.com/api/v1/game/99113/boxscore"
        session = build_http_session()

        with patch.object(
            requests.Session,
            "get",
            return_value=_response(url, b'{"message": "not found"}', status=404),
        ) as upstream:
            response = session.get(url)

        self.assertEqual(response.status_code, 404)
        upstream.assert_called_once_with(url)


if __name__ == "__main__":
    unittest.main()
