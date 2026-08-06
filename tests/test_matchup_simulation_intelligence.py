import unittest

from matchup_simulation_intelligence import (
    SIMULATION_VERSION,
    build_simulation_signal,
    exact_over_probability,
    simulation_audit,
    summarize_game_outcomes,
    wilson_interval,
)


class MatchupSimulationIntelligenceTests(unittest.TestCase):
    def test_exact_over_probability_uses_the_actual_sportsbook_line(self):
        outcomes = [4, 5, 5, 6, 7]
        self.assertAlmostEqual(exact_over_probability(outcomes, 5.5), .4)
        self.assertAlmostEqual(exact_over_probability(outcomes, 4.5), .8)

    def test_game_outcomes_link_moneyline_to_the_same_run_trials(self):
        summary = summarize_game_outcomes(
            [5, 3, 4, 2],
            [2, 4, 4, 2],
        )
        # One away win, one home win, and two ties split evenly.
        self.assertAlmostEqual(summary['awayWinProbability'], .5)
        self.assertAlmostEqual(summary['homeWinProbability'], .5)
        self.assertAlmostEqual(summary['tieProbability'], .5)
        self.assertAlmostEqual(summary['awayMeanRuns'], 3.5)
        self.assertAlmostEqual(summary['homeMeanRuns'], 3.0)

    def test_shared_signal_includes_interval_trials_and_matchup_source(self):
        signal = build_simulation_signal(
            .64,
            1500,
            mode='linked_plate_appearance_game_simulation',
            matchup='Hitter versus opposing starter and bullpen',
            outcome_mean=1.12,
        )
        self.assertTrue(signal['sharedSimulationBacked'])
        self.assertEqual(
            signal['matchupSimulationVersion'], SIMULATION_VERSION
        )
        self.assertEqual(signal['gameSimN'], 1500)
        self.assertLess(signal['gameSimPlo'], .64)
        self.assertGreater(signal['gameSimPhi'], .64)
        self.assertAlmostEqual(signal['gameSimMean'], 1.12)
        self.assertIn(
            'opposing starter and bullpen',
            signal['matchupSimulationSource'],
        )

    def test_wilson_interval_tightens_as_trial_count_grows(self):
        small = wilson_interval(.60, 100)
        large = wilson_interval(.60, 2500)
        self.assertGreater(small[1] - small[0], large[1] - large[0])

    def test_simulation_audit_reports_shared_coverage(self):
        rows = [
            build_simulation_signal(.62, 1500, mode='hitter', matchup='A vs B'),
            build_simulation_signal(.58, 1500, mode='pitcher', matchup='C vs lineup'),
            {'marketKey': 'h2h'},
        ]
        self.assertEqual(simulation_audit(rows), {
            'candidateCount': 3,
            'simulationBackedCount': 2,
            'simulationCoveragePct': 66.7,
            'minimumLinkedTrials': 1500,
            'versions': [SIMULATION_VERSION],
        })


if __name__ == '__main__':
    unittest.main()
