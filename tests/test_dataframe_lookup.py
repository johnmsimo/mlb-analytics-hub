import unittest
from unittest.mock import patch

import pandas as pd

import fg_stuff_loader
import framing_loader
import savant_bat_tracking
from dataframe_lookup import DataFrameLookupIndex


class DataFrameLookupIndexTests(unittest.TestCase):
    def test_id_and_name_formats_resolve_same_row(self):
        df = pd.DataFrame(
            [
                {"id": 13.0, "name": "Simo, John", "value": 99},
                {"id": 27, "name": "Mike Trout", "value": 27},
            ]
        )
        index = DataFrameLookupIndex(
            df,
            id_columns=("id",),
            name_columns=("name",),
        )

        self.assertEqual(index.find(player_id="13").get("value"), 99)
        self.assertEqual(index.find(name="John Simo").get("value"), 99)
        self.assertEqual(index.find(name="simo, john").get("value"), 99)

    def test_last_name_fallback_retains_first_row_behavior(self):
        df = pd.DataFrame(
            [
                {"name": "Aaron Judge", "value": 1},
                {"name": "John Judge", "value": 2},
            ]
        )
        index = DataFrameLookupIndex(df, name_columns=("name",))

        row = index.find(name="Judge", last_name_fallback=True)

        self.assertEqual(row.get("value"), 1)

    def test_missing_lookup_returns_none(self):
        index = DataFrameLookupIndex(
            pd.DataFrame([{"id": 1, "name": "Known Player"}]),
            id_columns=("id",),
            name_columns=("name",),
        )

        self.assertIsNone(index.find(player_id=2, name="Missing Player"))


class IndexedLoaderContractTests(unittest.TestCase):
    def tearDown(self):
        for slot in savant_bat_tracking._cache.values():
            slot.update({"df": None, "loaded_at": None, "year": None, "index": None})
        fg_stuff_loader._cache.update(
            {"df": None, "loaded_at": None, "year": None, "index": None}
        )
        framing_loader._cache.update(
            {"df": None, "loaded_at": None, "year": None, "index": None}
        )

    def test_savant_last_name_and_id_lookups_preserve_contract(self):
        df = pd.DataFrame(
            [
                {
                    "id": 13,
                    "name": "Simo",
                    "avg_bat_speed": 75.0,
                    "squared_up_per_swing": 40.0,
                },
                {
                    "id": 27,
                    "name": "Trout",
                    "avg_bat_speed": 70.0,
                    "squared_up_per_swing": 30.0,
                },
            ]
        )

        with patch.object(savant_bat_tracking, "_load", return_value=df):
            by_name = savant_bat_tracking.bat_tracking(name="John Simo")
            by_id = savant_bat_tracking.bat_tracking(player_id="13")

        self.assertEqual(by_name["bat_speed"], 75.0)
        self.assertEqual(by_id["squared_up_pct"], 40.0)

    def test_stuff_lookup_rebuilds_when_dataframe_refreshes(self):
        first = pd.DataFrame(
            [{"playerid": "13", "Name": "John Simo", "Stuff+": 101.0}]
        )
        second = pd.DataFrame(
            [{"playerid": "13", "Name": "John Simo", "Stuff+": 112.0}]
        )

        with patch.object(fg_stuff_loader, "_load", return_value=first):
            first_row = fg_stuff_loader.fg_stuff(player_id=13)
        with patch.object(fg_stuff_loader, "_load", return_value=second):
            second_row = fg_stuff_loader.fg_stuff(player_id=13)

        self.assertEqual(first_row["stuff_plus"], 101.0)
        self.assertEqual(second_row["stuff_plus"], 112.0)

    def test_framing_lookup_supports_comma_name_and_integer_id(self):
        df = pd.DataFrame(
            [
                {
                    "id": 13.0,
                    "last_name, first_name": "Simo, John",
                    "rv_tot": 4.5,
                }
            ]
        )

        with patch.object(framing_loader, "_load", return_value=df):
            by_name = framing_loader.framing_runs(name="John Simo")
            by_id = framing_loader.framing_runs(player_id="13")

        self.assertEqual(by_name["framing_runs"], 4.5)
        self.assertEqual(by_id["framing_runs"], 4.5)


if __name__ == "__main__":
    unittest.main()
