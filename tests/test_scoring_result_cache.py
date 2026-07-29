import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import numpy as np

import xgb_prop_scorer as scorer
from scoring_result_cache import ScoringResultCache


class ScoringResultCacheTests(unittest.TestCase):
    def test_reuses_result_and_returns_copy(self):
        cache = ScoringResultCache()
        calls = []

        def compute():
            calls.append(True)
            return {"nested": {"value": 7}}

        first = cache.get_or_compute("same", compute, ttl_seconds=60, max_entries=4)
        first["nested"]["value"] = 99
        second = cache.get_or_compute("same", compute, ttl_seconds=60, max_entries=4)

        self.assertEqual(second, {"nested": {"value": 7}})
        self.assertEqual(len(calls), 1)
        self.assertEqual(cache.status()["hits"], 1)

    def test_ttl_and_lru_bound(self):
        now = [10.0]
        cache = ScoringResultCache(clock=lambda: now[0])

        cache.get_or_compute("a", lambda: 1, ttl_seconds=5, max_entries=2)
        cache.get_or_compute("b", lambda: 2, ttl_seconds=5, max_entries=2)
        cache.get_or_compute("c", lambda: 3, ttl_seconds=5, max_entries=2)
        self.assertEqual(cache.status()["entries"], 2)
        self.assertEqual(cache.status()["evictions"], 1)

        now[0] = 16.0
        self.assertEqual(
            cache.get_or_compute("b", lambda: 20, ttl_seconds=5, max_entries=2),
            20,
        )
        self.assertEqual(cache.status()["expirations"], 1)

    def test_concurrent_identical_work_is_singleflight(self):
        cache = ScoringResultCache()
        calls = []
        started = threading.Event()
        release = threading.Event()

        def compute():
            calls.append(True)
            started.set()
            release.wait(timeout=2)
            return {"value": 1}

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(
                    cache.get_or_compute,
                    "same",
                    compute,
                    ttl_seconds=60,
                    max_entries=4,
                )
                for _ in range(4)
            ]
            self.assertTrue(started.wait(timeout=1))
            time.sleep(0.05)
            release.set()
            results = [future.result(timeout=2) for future in futures]

        self.assertEqual(results, [{"value": 1}] * 4)
        self.assertEqual(len(calls), 1)
        self.assertEqual(cache.status()["waits"], 3)

    def test_failed_computation_is_not_cached_or_left_inflight(self):
        cache = ScoringResultCache()

        with self.assertRaisesRegex(RuntimeError, "boom"):
            cache.get_or_compute(
                "key",
                lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                ttl_seconds=60,
                max_entries=4,
            )

        self.assertEqual(cache.status()["inflight"], 0)
        self.assertEqual(
            cache.get_or_compute("key", lambda: 9, ttl_seconds=60, max_entries=4),
            9,
        )


class XgbScorerCacheIntegrationTests(unittest.TestCase):
    def setUp(self):
        scorer.xgb_score_cache_clear(reset_metrics=True)

    def tearDown(self):
        scorer.xgb_score_cache_clear(reset_metrics=True)

    def test_prob_scoring_reuses_identical_features_and_isolates_changes(self):
        class FakeModel:
            def __init__(self):
                self.calls = 0

            def predict_proba(self, features):
                self.calls += 1
                return np.array([[0.25, 0.75]])

        model = FakeModel()
        first_features = np.array([[1.0, 2.0]], dtype=np.float32)
        changed_features = np.array([[1.0, 3.0]], dtype=np.float32)

        with (
            patch.dict(
                os.environ,
                {
                    "XGB_SCORE_CACHE_TTL": "60",
                    "XGB_SCORE_CACHE_MAX_ENTRIES": "8",
                },
            ),
            patch.dict(scorer._models, {"hits": model}, clear=True),
            patch.object(scorer, "_xgb_calibrated", return_value=True),
            patch.object(scorer, "_apply_isotonic", side_effect=lambda p, _m: p),
        ):
            self.assertEqual(
                scorer._score_prob("hits", "batter_hits", first_features),
                0.75,
            )
            self.assertEqual(
                scorer._score_prob("hits", "batter_hits", first_features.copy()),
                0.75,
            )
            self.assertEqual(
                scorer._score_prob("hits", "batter_hits", changed_features),
                0.75,
            )

        self.assertEqual(model.calls, 2)

    def test_full_scoring_reuses_interval_and_monte_carlo(self):
        class FakeModel:
            def __init__(self):
                self.calls = 0

            def predict_proba(self, features):
                self.calls += 1
                return np.array([[0.4, 0.6]])

        model = FakeModel()
        features = np.array([[4.0, 5.0]], dtype=np.float32)

        with (
            patch.dict(
                os.environ,
                {
                    "XGB_SCORE_CACHE_TTL": "60",
                    "XGB_SCORE_CACHE_MAX_ENTRIES": "8",
                },
            ),
            patch.dict(scorer._models, {"hits": model}, clear=True),
            patch.object(scorer, "_xgb_calibrated", return_value=True),
            patch.object(scorer, "_apply_isotonic", side_effect=lambda p, _m: p),
            patch.object(scorer, "_xgb_interval", return_value=(0.5, 0.7)) as interval,
            patch.object(scorer, "mc_simulate", return_value={"mc_prob_over": 0.61}) as mc,
        ):
            first = scorer._score_full(
                "hits", "batter_hits", features, line=0.5
            )
            first["mc"]["mc_prob_over"] = 0
            second = scorer._score_full(
                "hits", "batter_hits", features.copy(), line=0.5
            )

        self.assertEqual(second["mc"]["mc_prob_over"], 0.61)
        self.assertEqual(model.calls, 1)
        interval.assert_called_once()
        mc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
