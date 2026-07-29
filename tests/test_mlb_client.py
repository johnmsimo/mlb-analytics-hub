from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

from clients.mlb_client import MLBClient


def _session_with(payload):
    response = Mock()
    response.status_code = 200
    response.json.return_value = payload
    session = Mock()
    session.get.return_value = response
    return session, response


class MLBClientTests(unittest.TestCase):
    def test_get_json_uses_configured_base_url_and_timeout(self):
        session, response = _session_with({"people": []})
        client = MLBClient(session=session)

        with patch.dict(
            os.environ,
            {
                "MLB_STATS_API_BASE_URL": "https://example.test/api/",
                "MLB_HTTP_TIMEOUT": "17",
            },
            clear=True,
        ):
            payload = client.get_json("people/13")

        self.assertEqual(payload, {"people": []})
        session.get.assert_called_once_with(
            "https://example.test/api/v1/people/13",
            params=None,
            timeout=17,
        )
        response.raise_for_status.assert_called_once_with()

    def test_schedule_flattens_all_date_blocks(self):
        session, _ = _session_with(
            {
                "dates": [
                    {"games": [{"gamePk": 1}]},
                    {"games": [{"gamePk": 2}]},
                ]
            }
        )
        client = MLBClient(session=session, base_url="https://example.test/api")

        games = client.schedule(
            date_str="2026-07-28",
            hydrate="team,venue",
            timeout=9,
        )

        self.assertEqual(games, [{"gamePk": 1}, {"gamePk": 2}])
        self.assertEqual(
            session.get.call_args.kwargs["params"],
            {
                "sportId": 1,
                "date": "2026-07-28",
                "hydrate": "team,venue",
            },
        )

    def test_person_stats_preserves_query_parameters(self):
        session, _ = _session_with({"stats": []})
        client = MLBClient(session=session, base_url="https://example.test/api")

        client.person_stats(
            13,
            stats="vsPlayer",
            opposingPlayerId=99,
            group="hitting",
        )

        self.assertEqual(
            session.get.call_args.kwargs["params"],
            {
                "stats": "vsPlayer",
                "opposingPlayerId": 99,
                "group": "hitting",
            },
        )

    def test_game_boxscore_uses_v1_endpoint(self):
        session, _ = _session_with({"teams": {}})
        client = MLBClient(session=session, base_url="https://example.test/api")

        payload = client.game_boxscore(99113, timeout=8)

        self.assertEqual(payload, {"teams": {}})
        session.get.assert_called_once_with(
            "https://example.test/api/v1/game/99113/boxscore",
            params=None,
            timeout=8,
        )

    def test_game_live_feed_uses_v1_1_endpoint(self):
        session, _ = _session_with({"liveData": {}})
        client = MLBClient(session=session, base_url="https://example.test/api")

        payload = client.game_live_feed(99113, timeout=9)

        self.assertEqual(payload, {"liveData": {}})
        session.get.assert_called_once_with(
            "https://example.test/api/v1.1/game/99113/feed/live",
            params=None,
            timeout=9,
        )

    def test_non_object_json_is_rejected(self):
        session, _ = _session_with([])
        client = MLBClient(session=session, base_url="https://example.test/api")

        with self.assertRaises(ValueError):
            client.get_json("schedule")

    def test_http_errors_are_not_swallowed(self):
        session, response = _session_with({})
        response.raise_for_status.side_effect = RuntimeError("upstream failed")
        client = MLBClient(session=session, base_url="https://example.test/api")

        with self.assertRaisesRegex(RuntimeError, "upstream failed"):
            client.get_json("schedule")


if __name__ == "__main__":
    unittest.main()
