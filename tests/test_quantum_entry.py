"""Tests for the entry rule in `xau/quantum.py`.

The 50% retrace is adverse selection: a limit that never fills is a limit the
price ran away from, so the plans that expire are the strongest ones.
`CONVERT_TO_MARKET_BARS` takes those at market instead. What has to stay true is
that a converted entry is priced by exactly the same rule as an immediate one —
1R must mean the same distance either way, or every R in the journal and the
backtest silently stops being comparable.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau import quantum  # noqa: E402


def frame(rows: int = 40, close: float = 4000.0, atr: float = 8.0) -> pd.DataFrame:
    """The columns `_convert_to_market` reads, and nothing else."""
    return pd.DataFrame({
        "time": pd.date_range("2026-06-01", periods=rows, freq="15min"),
        "open": close, "high": close + 1, "low": close - 1, "close": close,
        "atr": atr, "up_level": close + 30.0, "down_level": close - 30.0,
    })


def plan(direction: int = 1) -> dict:
    return {"direction": direction, "side": "long" if direction == 1 else "short",
            "signal_index": 10, "entry_fill_index": None, "entry": 3990.0,
            "stop": 3974.0, "risk": 16.0, "targets": [4006.0, 4014.0, 4022.0],
            "resolved": [None, None, None], "pending": True, "active": False,
            "converted": False, "status": "BUY WAIT ENTRY"}


class ConvertToMarketTests(unittest.TestCase):

    def test_the_entry_moves_to_the_conversion_bar_close(self):
        d = frame()
        d.loc[12, "close"] = 4012.0
        converted = quantum._convert_to_market(plan(), d, 12)
        self.assertEqual(converted["entry"], 4012.0)
        self.assertEqual(converted["entry_fill_index"], 12)
        self.assertTrue(converted["active"])
        self.assertFalse(converted["pending"])
        self.assertTrue(converted["converted"])

    def test_the_stop_follows_the_entry_so_one_r_keeps_its_meaning(self):
        d = frame()
        d.loc[12, "close"] = 4012.0
        converted = quantum._convert_to_market(plan(), d, 12)
        self.assertAlmostEqual(abs(converted["entry"] - converted["stop"]),
                               converted["risk"], places=9)
        for rr, target in zip(quantum.RR_TARGETS, converted["targets"]):
            self.assertAlmostEqual(target, converted["entry"] + converted["risk"] * rr,
                                   places=9)

    def test_the_risk_stays_inside_the_atr_bounds(self):
        # Structure far below the entry would ask for a stop much wider than the
        # ceiling; the clamp is what stops one setup carrying five times the risk.
        d = frame(atr=8.0)
        d.loc[12, "down_level"] = 3500.0
        converted = quantum._convert_to_market(plan(), d, 12)
        self.assertLessEqual(converted["risk"], 8.0 * quantum.MAX_STOP_ATR + 1e-9)
        d.loc[12, "down_level"] = 3999.9
        converted = quantum._convert_to_market(plan(), d, 12)
        self.assertGreaterEqual(converted["risk"], 8.0 * quantum.MIN_STOP_ATR - 1e-9)

    def test_a_short_converts_the_same_way_in_the_other_direction(self):
        d = frame()
        d.loc[12, "close"] = 3988.0
        converted = quantum._convert_to_market(plan(direction=-1), d, 12)
        self.assertEqual(converted["entry"], 3988.0)
        self.assertGreater(converted["stop"], converted["entry"])
        self.assertLess(converted["targets"][0], converted["entry"])
        self.assertAlmostEqual(abs(converted["entry"] - converted["stop"]),
                               converted["risk"], places=9)

    def test_the_limit_it_was_waiting_on_is_kept_for_reconstruction(self):
        """`ai_historical` rebuilds the decision as of the signal bar and must not
        read entry/stop/targets that were priced off the conversion bar."""
        d = frame()
        d.loc[12, "close"] = 4012.0
        original = plan()
        before = (original["entry"], original["stop"], original["risk"],
                  list(original["targets"]))
        converted = quantum._convert_to_market(original, d, 12)
        self.assertEqual(converted["limit_entry"], before[0])
        self.assertEqual(converted["limit_stop"], before[1])
        self.assertEqual(converted["limit_risk"], before[2])
        self.assertEqual(converted["limit_targets"], before[3])
        self.assertNotEqual(converted["entry"], converted["limit_entry"])

    def test_an_immediate_plan_is_priced_the_same_as_a_converted_one(self):
        """Both go through `_stop_and_risk`, so the same bar must give the same stop."""
        d = frame()
        d.loc[12, "close"] = 4012.0
        direct_stop, direct_risk = quantum._stop_and_risk(d, 12, 1, 4012.0)
        converted = quantum._convert_to_market(plan(), d, 12)
        self.assertAlmostEqual(converted["stop"], direct_stop, places=9)
        self.assertAlmostEqual(converted["risk"], direct_risk, places=9)


class ConversionSwitchTests(unittest.TestCase):
    """`CONVERT_TO_MARKET_BARS = None` has to restore the original behaviour."""

    def setUp(self):
        self.original = quantum.CONVERT_TO_MARKET_BARS

    def tearDown(self):
        quantum.CONVERT_TO_MARKET_BARS = self.original

    def _run(self, setting):
        quantum.CONVERT_TO_MARKET_BARS = setting
        data = pd.read_csv(ROOT / "data" / "market" / "XAUUSD" / "M30.csv")
        return quantum.analyse(data.tail(6000), "M30")

    def _counts(self, setting):
        result = self._run(setting)
        return result["counts"], result["plans"]

    def test_switching_off_restores_expiring_limits(self):
        counts, plans = self._counts(None)
        self.assertGreater(counts["expired"], 0)
        self.assertEqual(counts["converted"], 0)
        self.assertFalse(any(p["converted"] for p in plans))

    def test_switching_on_converts_instead_of_expiring(self):
        counts, plans = self._counts(2)
        self.assertEqual(counts["expired"], 0)
        self.assertGreater(counts["converted"], 0)
        converted = [p for p in plans if p["converted"]]
        # Every conversion happens on the promised bar, never earlier or later.
        self.assertTrue(all(p["entry_fill_index"] - p["signal_index"] == 2
                            for p in converted))

    def test_a_reversal_during_the_wait_cancels_rather_than_converting(self):
        """The reversal guard keeps its say; only stragglers convert.

        Asserted on the plans themselves rather than on the cancel counter: a
        sample that happens to contain no reversal inside the two-bar window
        would make a counter assertion pass while proving nothing.
        """
        result = self._run(2)
        events = result["data"]["break_event"].to_numpy()
        for plan_ in (p for p in result["plans"] if p["converted"]):
            during = events[plan_["signal_index"] + 1:plan_["entry_fill_index"] + 1]
            self.assertNotIn(-plan_["direction"], list(during),
                             f"plan at bar {plan_['signal_index']} converted despite "
                             "an opposite break that should have cancelled it")


if __name__ == "__main__":
    unittest.main()
