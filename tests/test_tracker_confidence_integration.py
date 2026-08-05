import types
import unittest

from tracker_confidence_integration import install_tracker_confidence


class TrackerConfidenceIntegrationTests(unittest.TestCase):
    def _module(self):
        module = types.ModuleType("fake_app")

        def _tracker_pick_payload(row):
            payload = dict(row)
            payload["sideLabel"] = "Over"
            return payload

        module._tracker_pick_payload = _tracker_pick_payload
        return module

    def test_install_adds_confidence_without_removing_tracker_fields(self):
        module = self._module()
        serializer = install_tracker_confidence(module)

        payload = serializer({
            "id": "pick-426",
            "adjProb": 0.64,
            "marketImplied": 0.52,
            "recommendedSide": "Over",
            "mc_prob_over": 0.63,
            "mc_std": 0.07,
            "mc_n_sims": 2500,
        })

        self.assertEqual(payload["id"], "pick-426")
        self.assertEqual(payload["sideLabel"], "Over")
        self.assertIn("confidenceScore", payload)
        self.assertIn("confidenceTier", payload)
        self.assertIn("confidenceLabel", payload)
        self.assertIn("confidenceExplanation", payload)
        self.assertIn("confidenceComponents", payload)

    def test_install_is_idempotent(self):
        module = self._module()
        first = install_tracker_confidence(module)
        second = install_tracker_confidence(module)

        self.assertIs(first, second)

    def test_missing_probability_returns_explicit_low_confidence(self):
        module = self._module()
        payload = install_tracker_confidence(module)({"id": "pick-no-probability"})

        self.assertEqual(payload["confidenceScore"], 0.0)
        self.assertEqual(payload["confidenceTier"], "LOW")
        self.assertIn("No calibrated probability", payload["confidenceExplanation"])


if __name__ == "__main__":
    unittest.main()
