import unittest

from xau import technique_lab


class TechniqueLabTests(unittest.TestCase):
    def test_breakeven_protects_remaining_size_after_tp1(self):
        plan = {"resolved": ["tp", "sl", "sl"]}
        weights = (.5, .25, .25)
        self.assertEqual(technique_lab._weighted_payoff(plan, weights, False), 0.0)
        self.assertEqual(technique_lab._weighted_payoff(plan, weights, True), 0.5)

    def test_weighted_tp3_payoff(self):
        plan = {"resolved": ["tp", "tp", "tp"]}
        value = technique_lab._weighted_payoff(plan, (.33, .33, .34), True)
        self.assertAlmostEqual(value, .33 * 1 + .33 * 1.5 + .34 * 2)

    def test_tp2_step_locks_the_tp3_weight_at_one_r(self):
        plan = {"resolved": ["tp", "tp", "sl"]}
        be_only = technique_lab._weighted_payoff(
            plan, (.33, .33, .34), True,
        )
        stepped = technique_lab._weighted_payoff(
            plan, (.33, .33, .34), True, step_after_tp2=True,
        )
        self.assertAlmostEqual(be_only, .33 * 1 + .33 * 1.5)
        self.assertAlmostEqual(stepped, be_only + .34)

    def test_summary_reports_drawdown_and_expectancy(self):
        result = technique_lab._summary([1.0, -1.0, 2.0, 0.0])
        self.assertEqual(result["trades"], 4)
        self.assertEqual(result["win_rate"], 50.0)
        self.assertEqual(result["expectancy_r"], 0.5)
        self.assertEqual(result["max_drawdown_r"], 1.0)


if __name__ == "__main__":
    unittest.main()
