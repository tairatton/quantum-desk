"""The FTMO portfolio simulator: objectives it was not enforcing."""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

TREE = Path(__file__).resolve().parents[2]
for entry in (TREE, TREE / "tools"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import numpy as np                                    # noqa: E402

import ftmo_portfolio_sim as sim                       # noqa: E402


class MinimumTradingDayTests(unittest.TestCase):
    """FTMO's objective is the target AND four trading days, per phase."""

    def test_a_one_day_target_hit_does_not_pass(self):
        # Regression: a +10% first day passed on day one, an outcome FTMO does
        # not offer, which flattered every "days to pass" figure the tool printed.
        passed, day, _ = sim.resolve_phase(np.array([[10.0, 0, 0, 0, 0]]), 10.0)
        self.assertFalse(bool(passed[0]))

    def test_the_earliest_pass_is_the_fourth_traded_day(self):
        passed, day, _ = sim.resolve_phase(
            np.array([[3.0, 3.0, 3.0, 3.0, 0.0]]), 10.0)
        self.assertTrue(bool(passed[0]))
        self.assertEqual(int(day[0]), 4)

    def test_flat_days_do_not_count_toward_the_minimum(self):
        # Target met on day 2, then nothing traded: the phase is not complete.
        passed, _, _ = sim.resolve_phase(
            np.array([[5.0, 5.0, 0.0, 0.0, 0.0]]), 10.0)
        self.assertFalse(bool(passed[0]))

    def test_the_minimum_can_be_disabled_for_experiments(self):
        passed, day, _ = sim.resolve_phase(np.array([[10.0, 0, 0, 0, 0]]), 10.0,
                                           min_days=0)
        self.assertTrue(bool(passed[0]))
        self.assertEqual(int(day[0]), 1)

    def test_target_must_still_be_met_on_the_minimum_day(self):
        # Regression: remembering the first target hit let a path pass after it
        # gave the profit back before its fourth trading day.
        passed, _, _ = sim.resolve_phase(
            np.array([[10.0, -4.0, -4.0, 0.1]]), 10.0)
        self.assertFalse(bool(passed[0]))

    def test_explicit_traded_mask_counts_zero_net_trading_days(self):
        days = np.array([[3.0, 3.0, 4.0, 0.0]])
        traded = np.array([[True, True, True, True]])
        passed, day, _ = sim.resolve_phase(days, 10.0, traded_mask=traded)
        self.assertTrue(bool(passed[0]))
        self.assertEqual(int(day[0]), 4)


class InternalStopTests(unittest.TestCase):
    """The live bot stands down at 1.50%; the simulation now does too."""

    def test_a_days_net_loss_is_clipped_at_the_internal_stop(self):
        clipped = sim.apply_internal_stop(np.array([[-3.0, 0.9]]))
        self.assertAlmostEqual(clipped[0][0], -1.5)
        self.assertAlmostEqual(clipped[0][1], 0.9)

    def test_the_approximation_is_documented(self):
        self.assertIn("APPROXIMATION", sim.apply_internal_stop.__doc__)


class EngineDependencyTests(unittest.TestCase):
    def test_no_engine_module_imports_bot(self):
        offenders = []
        for path in (TREE / "engine").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.ImportFrom)
                        and (node.module or "").startswith("bot")):
                    offenders.append(f"{path.name}:{node.lineno}")
                if isinstance(node, ast.Import):
                    offenders.extend(
                        f"{path.name}:{node.lineno}" for alias in node.names
                        if alias.name.startswith("bot"))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
