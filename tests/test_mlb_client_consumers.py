from __future__ import annotations

import unittest
from unittest.mock import patch

import bq_etl
import lineup_loader
import pipeline_scheduler
import umpire_loader
import weather_loader


class MLBClientConsumerTests(unittest.TestCase):
    def test_lineup_schedule_contract_is_preserved(self):
        games = [{"gamePk": 13}]
        with patch.object(
            lineup_loader.mlb_client,
            "schedule",
            return_value=games,
        ) as schedule:
            result = lineup_loader._fetch_games_for_date("2026-07-28")

        self.assertEqual(result, games)
        schedule.assert_called_once_with(
            date_str="2026-07-28",
            hydrate="lineups,probablePitcher,team",
            timeout=10,
        )

    def test_umpire_schedule_is_parsed_without_shape_change(self):
        games = [
            {
                "gamePk": 13,
                "officials": [
                    {
                        "officialType": "Home Plate",
                        "official": {"fullName": "Pat Hoberg"},
                    }
                ],
            }
        ]
        with patch.object(umpire_loader.mlb_client, "schedule", return_value=games):
            result = umpire_loader._fetch_game_officials("2026-07-28")

        self.assertEqual(result, {13: "Pat Hoberg"})

    def test_weather_schedule_is_parsed_without_shape_change(self):
        games = [
            {
                "gamePk": 13,
                "teams": {
                    "home": {"team": {"abbreviation": "NYY"}},
                },
            }
        ]
        with patch.object(weather_loader.mlb_client, "schedule", return_value=games):
            result = weather_loader._fetch_home_teams("2026-07-28")

        self.assertEqual(result, {13: "NYY"})

    def test_pipeline_roster_filters_pitchers(self):
        payload = {
            "roster": [
                {"person": {"id": 1}, "position": {"type": "Infielder"}},
                {"person": {"id": 2}, "position": {"type": "Pitcher"}},
            ]
        }
        with patch.object(
            pipeline_scheduler.mlb_client,
            "team_roster",
            return_value=payload,
        ):
            result = pipeline_scheduler._get_position_player_ids(147)

        self.assertEqual(result, [1])

    def test_bigquery_bulk_stats_contract_is_preserved(self):
        payload = {
            "stats": [
                {"splits": [{"player": {"id": 1}}]},
                {"splits": [{"player": {"id": 2}}]},
            ]
        }
        with patch.object(
            bq_etl.mlb_client,
            "stats",
            return_value=payload,
        ) as stats:
            result = bq_etl._fetch_stats_bulk("hitting", season=2026)

        self.assertEqual([row["player"]["id"] for row in result], [1, 2])
        self.assertEqual(stats.call_args.kwargs["group"], "hitting")
        self.assertEqual(stats.call_args.kwargs["season"], 2026)


if __name__ == "__main__":
    unittest.main()
