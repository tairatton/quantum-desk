"""The TopStep simulator: the parts that were quietly reporting fiction."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for entry in (ROOT, ROOT / "tools"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import numpy as np                                    # noqa: E402

import topstep_sim as sim                             # noqa: E402


class LadderWinningDayTests(unittest.TestCase):
    """Regression: ladder mode accepted the flag and ignored it."""

    def setUp(self):
        self._nsim, self._days = sim.NSIM, sim.MAXDAYS
        sim.NSIM, sim.MAXDAYS = 300, 60

    def tearDown(self):
        sim.NSIM, sim.MAXDAYS = self._nsim, self._days

    def test_an_impossible_requirement_cannot_be_passed(self):
        loose = sim.simulate_ladder("holdout", 0.3, 400.0, require_winning_days=0)
        strict = sim.simulate_ladder("holdout", 0.3, 400.0,
                                     require_winning_days=10_000)
        self.assertGreater(loose["pass"], 0.5)
        self.assertEqual(strict["pass"], 0.0)

    def test_a_stricter_requirement_never_passes_more_paths(self):
        easy = sim.simulate_ladder("holdout", 0.3, 400.0, require_winning_days=1)
        hard = sim.simulate_ladder("holdout", 0.3, 400.0, require_winning_days=5)
        self.assertLessEqual(hard["pass"], easy["pass"])

    def test_simulator_ladder_is_the_production_ladder(self):
        from bot.settings import Settings
        from engine.dynamic_risk import ladder_steps

        self.assertEqual(sim.LADDER, ladder_steps(Settings()))
        self.assertEqual(sim.ladder_risk(np.array([1500.0]))[0], 50.0)


class ResolvedPathStatisticsTests(unittest.TestCase):
    """Statistics must stop at the day the account passed or blew up.

    Regression: drawdown, best day and worst day were measured across all 400
    generated days, including days that never happened because the evaluation
    was already over. Pinning the ladder to a single tier makes the two modes
    the same experiment, so every reported number must agree.
    """

    def setUp(self):
        self._nsim, self._days, self._ladder = sim.NSIM, sim.MAXDAYS, sim.LADDER
        sim.NSIM, sim.MAXDAYS = 500, 80
        sim.LADDER = ((float("inf"), 200.0),)

    def tearDown(self):
        sim.NSIM, sim.MAXDAYS, sim.LADDER = self._nsim, self._days, self._ladder

    def test_fixed_and_ladder_modes_agree_when_the_ladder_has_one_tier(self):
        fixed = sim.simulate("holdout", 200.0, 0.3, 0, 400.0)
        ladder = sim.simulate_ladder("holdout", 0.3, 400.0, 0)
        for key in ("pass", "fail", "days_med", "days_p90", "dd_med",
                    "best_day_med", "worst_day"):
            self.assertAlmostEqual(fixed[key], ladder[key], places=6, msg=key)

    def test_verdicts_always_partition(self):
        result = sim.simulate("holdout", 200.0, 0.3, 0, 400.0)
        total = result["pass"] + result["fail"] + result["unresolved"]
        self.assertAlmostEqual(total, 1.0, places=6)


class DailyStopApproximationTests(unittest.TestCase):
    """The lockout is clipped on net, and the report has to say so."""

    def test_a_days_net_loss_is_clipped_at_the_tighter_stop(self):
        clipped = sim.apply_daily_stops(np.array([[-900.0, 100.0]]), 400.0)
        self.assertAlmostEqual(clipped[0][0], -400.0)

    def test_the_documented_limitation_is_stated_in_the_docstring(self):
        # The known-wrong case: -500 then +800 nets +300 and is left alone,
        # although the live bot would have locked out at -400.
        self.assertIn("APPROXIMATION", sim.apply_daily_stops.__doc__)
        self.assertAlmostEqual(
            sim.apply_daily_stops(np.array([[300.0]]), 400.0)[0][0], 300.0)


if __name__ == "__main__":
    unittest.main()
