"""Decision-policy tests for the live forward-evidence helper."""
from __future__ import annotations

import unittest

from scripts import forward_check


class ForwardCheckPolicyTests(unittest.TestCase):
    def test_confirmed_edge_keeps_the_approved_risk(self):
        label, advice = forward_check.verdict(
            forward_check.MIN_TRADES, forward_check.CONFIRM_R)
        self.assertIn("edge", label)
        self.assertIn("0.40%", advice)
        self.assertNotIn("0.60%", advice)

    def test_late_actual_risk_supersedes_conservative_open_value(self):
        trades = forward_check.closed_trades([
            {"event": "trade_opened", "plan_id": "p", "risk_cash": 200.0},
            {"event": "risk_cash_actualized", "plan_id": "p", "risk_cash": 160.0},
            {"event": "trade_closed", "plan_id": "p", "r": 1.0},
        ])
        self.assertEqual(trades[0]["risk_cash"], 160.0)


if __name__ == "__main__":
    unittest.main()
