import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pandas as pd

import fangraphs_loader


class FangraphsLookupIndexTests(unittest.TestCase):
    def setUp(self):
        self.original_cache = fangraphs_loader._cache
        self.original_indexes = fangraphs_loader._index_cache
        self.original_memo = fangraphs_loader._LOOKUP_MEMO
        fangraphs_loader._cache = {}
        fangraphs_loader._index_cache = {}
        fangraphs_loader._LOOKUP_MEMO = {}

    def tearDown(self):
        fangraphs_loader._cache = self.original_cache
        fangraphs_loader._index_cache = self.original_indexes
        fangraphs_loader._LOOKUP_MEMO = self.original_memo

    def _install(self, **frames):
        fangraphs_loader._cache.update(frames)
        fangraphs_loader._load_all()

    def test_id_exact_name_and_partial_name_preserve_first_match(self):
        self._install(
            bat=pd.DataFrame(
                [
                    {"playerid": "13", "Name": "John Simo", "PA": 30},
                    {"playerid": "27", "Name": "Johnny Simo", "PA": 40},
                ]
            )
        )

        self.assertEqual(
            fangraphs_loader._indexed_row("bat", player_id=13)["Name"],
            "John Simo",
        )
        self.assertEqual(
            fangraphs_loader._indexed_row("bat", name="Johnny Simo")["playerid"],
            "27",
        )
        self.assertEqual(
            fangraphs_loader._indexed_row("bat", name="Simo")["playerid"],
            "13",
        )

    def test_stats_fallback_keeps_newer_values_and_older_sample(self):
        self._install(
            bat=pd.DataFrame(
                [{"playerid": "13", "Name": "John Simo", "PA": 5, "wOBA": 0.4}]
            ),
            bat_2025=pd.DataFrame(
                [{"playerid": "13", "Name": "John Simo", "PA": 200, "wOBA": 0.3, "ISO": 0.2}]
            ),
        )

        result = fangraphs_loader._get_stats_with_fallback(
            ["bat", "bat_2025"],
            "bat",
            player_id=13,
        )

        self.assertEqual(result["PA"], 5)
        self.assertEqual(result["wOBA"], 0.4)
        self.assertEqual(result["ISO"], 0.2)

    def test_projection_fallback_uses_first_matching_season(self):
        self._install(
            proj_bat=pd.DataFrame(columns=["playerid", "Name", "HR"]),
            proj_bat_2025=pd.DataFrame(
                [{"playerid": "13", "Name": "John Simo", "HR": 25}]
            ),
        )

        result = fangraphs_loader._get_proj_with_fallback(
            ["proj_bat", "proj_bat_2025"],
            name="John Simo",
        )

        self.assertEqual(result["HR"], 25)

    def test_repeated_and_concurrent_lookups_build_one_index(self):
        frame = pd.DataFrame(
            [
                {"playerid": str(player_id), "Name": f"Player {player_id}", "PA": 50}
                for player_id in range(1, 41)
            ]
        )
        fangraphs_loader._cache["bat"] = frame
        real_builder = fangraphs_loader._build_index

        with (
            patch.object(
                fangraphs_loader,
                "_build_index",
                wraps=real_builder,
            ) as build,
            ThreadPoolExecutor(max_workers=8) as pool,
        ):
            rows = list(
                pool.map(
                    lambda player_id: fangraphs_loader._indexed_row(
                        "bat",
                        player_id=player_id,
                    ),
                    range(1, 41),
                )
            )

        self.assertEqual(build.call_count, 1)
        self.assertEqual(len(rows), 40)
        self.assertTrue(all(row is not None for row in rows))

    def test_replacing_dataframe_rebuilds_index(self):
        first = pd.DataFrame(
            [{"playerid": "13", "Name": "John Simo", "PA": 30, "wOBA": 0.4}]
        )
        second = pd.DataFrame(
            [{"playerid": "13", "Name": "John Simo", "PA": 30, "wOBA": 0.35}]
        )
        self._install(bat=first)

        self.assertEqual(
            fangraphs_loader.get_batter_stats(player_id=13)["wOBA"],
            0.4,
        )
        fangraphs_loader._cache["bat"] = second

        self.assertEqual(
            fangraphs_loader.get_batter_stats(player_id=13)["wOBA"],
            0.35,
        )

    def test_public_results_remain_mutation_isolated_and_negative_cached(self):
        self._install(
            bat=pd.DataFrame(
                [{"playerid": "13", "Name": "John Simo", "PA": 30, "wOBA": 0.4}]
            )
        )
        with patch.object(
            fangraphs_loader,
            "_get_stats_with_fallback",
            wraps=fangraphs_loader._get_stats_with_fallback,
        ) as build:
            first = fangraphs_loader.get_batter_stats(player_id=13)
            first["wOBA"] = 0.1
            second = fangraphs_loader.get_batter_stats(player_id=13)
            missing_first = fangraphs_loader.get_batter_stats(player_id=999)
            missing_second = fangraphs_loader.get_batter_stats(player_id=999)

        self.assertEqual(second["wOBA"], 0.4)
        self.assertEqual(missing_first, {})
        self.assertEqual(missing_second, {})
        self.assertEqual(build.call_count, 2)
