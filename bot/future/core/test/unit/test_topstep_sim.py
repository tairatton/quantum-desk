"""The TopStep simulator: the parts that were quietly reporting fiction."""
from __future__ import annotations

import sys
import tempfile
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
        self._mgc_pools = sim._mgc_daily_pools
        sim.NSIM, sim.MAXDAYS = 300, 60
        sim._mgc_daily_pools = lambda risk: [
            np.full(max(sim.MAXDAYS, 100), float(risk) * 0.5),
            np.full(max(sim.MAXDAYS, 100), float(risk) * 0.5),
        ]

    def tearDown(self):
        sim.NSIM, sim.MAXDAYS = self._nsim, self._days
        sim._mgc_daily_pools = self._mgc_pools

    def test_an_impossible_requirement_cannot_be_passed(self):
        loose = sim.simulate_ladder(sim.MGC_SCENARIO, 0.3, 400.0, require_winning_days=0)
        strict = sim.simulate_ladder(sim.MGC_SCENARIO, 0.3, 400.0,
                                     require_winning_days=10_000)
        self.assertGreater(loose["pass"], 0.5)
        self.assertEqual(strict["pass"], 0.0)

    def test_a_stricter_requirement_never_passes_more_paths(self):
        easy = sim.simulate_ladder(sim.MGC_SCENARIO, 0.3, 400.0, require_winning_days=1)
        hard = sim.simulate_ladder(sim.MGC_SCENARIO, 0.3, 400.0, require_winning_days=5)
        self.assertLessEqual(hard["pass"], easy["pass"])

    def test_simulator_ladder_is_the_production_ladder(self):
        from bot.settings import Settings
        from engine.dynamic_risk import ladder_steps

        self.assertEqual(sim.LADDER, ladder_steps(Settings()))
        self.assertEqual(sim.ladder_risk(np.array([1500.0]))[0], 50.0)

    def test_simulator_limits_are_loaded_from_production_settings(self):
        from bot.settings import load

        settings = load()
        self.assertEqual(sim.ACCOUNT_SIZE, settings.account_size)
        self.assertEqual(sim.PROFIT_TARGET, settings.profit_target_dollars)
        self.assertEqual(sim.MAX_LOSS_LIMIT, settings.max_loss_limit_dollars)
        self.assertEqual(sim.DAILY_LOSS_LIMIT, settings.daily_loss_limit_dollars)
        self.assertEqual(sim.INTERNAL_DAILY_STOP,
                         settings.internal_daily_stop_dollars)
        self.assertEqual(sim.LOSS_ROOM_RESERVE,
                         settings.loss_room_reserve_dollars)
        self.assertEqual(sim.MGC_SCENARIO, "yahoo_60d")

    def test_cli_defaults_to_the_bot_room_aware_ladder(self):
        self.assertEqual(sim.select_risks(), [-2.0])
        self.assertEqual(sim.select_risks([200.0]), [200.0])
        self.assertEqual(sim.select_risks(flat=True), list(sim.DEFAULT_FIXED_RISKS))
        with self.assertRaisesRegex(ValueError, "choose only one"):
            sim.select_risks(ladder=True, room_aware=True)

    def test_future_sim_rejects_forex_scenarios(self):
        with self.assertRaisesRegex(ValueError, "Forex/XAUUSD"):
            sim.daily_dollars("holdout", 200.0, 0.3,
                              np.random.default_rng(41))


class ResolvedPathStatisticsTests(unittest.TestCase):
    """Statistics must stop at the day the account passed or blew up.

    Regression: drawdown, best day and worst day were measured across all 400
    generated days, including days that never happened because the evaluation
    was already over. Pinning the ladder to a single tier makes the two modes
    the same experiment, so every reported number must agree.
    """

    def setUp(self):
        self._nsim, self._days, self._ladder = sim.NSIM, sim.MAXDAYS, sim.LADDER
        self._mgc_pools = sim._mgc_daily_pools
        sim.NSIM, sim.MAXDAYS = 500, 80
        sim.LADDER = ((float("inf"), 200.0),)
        sim._mgc_daily_pools = lambda risk: [
            np.full(max(sim.MAXDAYS, 100), float(risk) * 0.5),
            np.full(max(sim.MAXDAYS, 100), float(risk) * 0.5),
        ]

    def tearDown(self):
        sim.NSIM, sim.MAXDAYS, sim.LADDER = self._nsim, self._days, self._ladder
        sim._mgc_daily_pools = self._mgc_pools

    def test_fixed_and_ladder_modes_agree_when_the_ladder_has_one_tier(self):
        fixed = sim.simulate(sim.MGC_SCENARIO, 200.0, 0.3, 0, 400.0)
        ladder = sim.simulate_ladder(sim.MGC_SCENARIO, 0.3, 400.0, 0)
        for key in ("pass", "fail", "days_med", "days_p90", "dd_med",
                    "best_day_med", "worst_day"):
            self.assertAlmostEqual(fixed[key], ladder[key], places=6, msg=key)

    def test_verdicts_always_partition(self):
        result = sim.simulate(sim.MGC_SCENARIO, 200.0, 0.3, 0, 400.0)
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


class MgcYahooSourceTests(unittest.TestCase):
    """The default futures path must use MGC data and real contract sizing."""

    def test_missing_mgc_cache_fails_with_download_instruction(self):
        old_dir = sim.MGC_DATA_DIR
        old_cache = sim._MGC_ANALYSIS.copy()
        sim.MGC_DATA_DIR = Path(tempfile.mkdtemp())
        sim._MGC_ANALYSIS.clear()
        try:
            with self.assertRaisesRegex(RuntimeError, "download_mgc_yahoo"):
                sim._load_mgc_analysis("M15")
        finally:
            sim.MGC_DATA_DIR = old_dir
            sim._MGC_ANALYSIS.clear()
            sim._MGC_ANALYSIS.update(old_cache)

    def test_mgc_trade_pnl_uses_whole_contracts_and_tp3(self):
        plan = {
            "entry": 2_000.0,
            "stop": 1_990.0,
            "resolved": ["tp", "tp", "tp"],
        }
        # $10/point × 10 points = $100 per contract; $200 requests two,
        # therefore the two-contract fixed-TP3 exit is priced at 2R.
        expected = 2 * 100 * 2 - 2 * (
            sim.PRODUCTION_SETTINGS.commission_per_contract
            + sim.PRODUCTION_SETTINGS.slippage_ticks
            * sim.PRODUCTION_SETTINGS.tick_value
        )
        self.assertAlmostEqual(sim._mgc_trade_pnl(plan, 200.0), expected)


if __name__ == "__main__":
    unittest.main()
