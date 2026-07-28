from __future__ import annotations

import unittest
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pipeline_cache_integration as integration


class PipelineCacheIntegrationTests(unittest.TestCase):
    def _module(self):
        calls = {"roster": 0, "bvp": 0, "splits": 0}

        def roster(team_id):
            calls["roster"] += 1
            return [team_id * 10]

        def bvp(batter_id, pitcher_id):
            calls["bvp"] += 1
            return {"bvp_pa": batter_id + pitcher_id}

        def splits(player_id, group="hitting"):
            calls["splits"] += 1
            return {"group": group, "player_id": player_id}

        module = SimpleNamespace(
            ET=ZoneInfo("America/New_York"),
            _get_position_player_ids=roster,
            _get_bvp=bvp,
            _get_platoon_splits=splits,
        )
        return module, calls

    def test_install_is_idempotent(self):
        module, _ = self._module()
        self.assertTrue(integration.install_pipeline_cache(module))
        self.assertFalse(integration.install_pipeline_cache(module))

    def test_repeated_loader_calls_compute_once(self):
        module, calls = self._module()
        integration.install_pipeline_cache(module)

        self.assertEqual(module._get_position_player_ids(99101), [991010])
        self.assertEqual(module._get_position_player_ids(99101), [991010])
        self.assertEqual(module._get_bvp(99102, 99103), {"bvp_pa": 198205})
        self.assertEqual(module._get_bvp(99102, 99103), {"bvp_pa": 198205})
        self.assertEqual(module._get_platoon_splits(99104), {"group": "hitting", "player_id": 99104})
        self.assertEqual(module._get_platoon_splits(99104), {"group": "hitting", "player_id": 99104})

        self.assertEqual(calls, {"roster": 1, "bvp": 1, "splits": 1})

    def test_schedule_keeps_dataframe_contract(self):
        module, _ = self._module()
        integration.install_pipeline_cache(module)
        calls = {"schedule": 0}

        def fetch_schedule(date_str):
            calls["schedule"] += 1
            return [{"game_pk": 99105, "date": date_str}]

        first = module._build_games_df(fetch_schedule, "2099-09-09")
        second = module._build_games_df(fetch_schedule, "2099-09-09")

        self.assertEqual(first.to_dict("records"), second.to_dict("records"))
        self.assertEqual(calls["schedule"], 1)


if __name__ == "__main__":
    unittest.main()
