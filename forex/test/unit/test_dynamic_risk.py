from __future__ import annotations

import tempfile
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import guardrails, run
from engine import dynamic_risk
from bot.broker import SymbolSpec
from bot.settings import Settings
from engine.sizing import pending_risk_percent
from engine.state import BotState


GOLD = SymbolSpec(
    name="XAUUSD", digits=2, point=0.01, volume_min=0.01,
    volume_max=100.0, volume_step=0.01, value_per_point=100.0,
    stops_level_points=0.0, filling=0,
)


def dynamic_settings(**changes):
    values = dict(
        dynamic_risk_enabled=True,
        risk_percent=0.40,
        dynamic_risk_max_percent=1.00,
        dynamic_risk_dd1_percent=0.50,
        dynamic_risk_tier2_percent=0.75,
        dynamic_risk_dd2_percent=1.00,
        dynamic_risk_tier3_percent=0.50,
        dynamic_risk_dd3_percent=1.50,
        max_open_risk_percent=1.50,
        max_risk_per_idea_percent=1.50,
    )
    values.update(changes)
    return Settings(**values)


class DynamicRiskDecisionTests(unittest.TestCase):
    def setUp(self):
        self.settings = dynamic_settings(initial_balance=50_000.0)
        self.state = BotState(initial_balance=50_000.0,
                              balance_high_water=50_000.0)

    def test_risk_steps_down_as_drawdown_increases(self):
        cases = (
            (50_000.0, 1.00),  # 0.00% DD
            (49_800.0, 1.00),  # 0.40% DD
            (49_700.0, 0.75),  # 0.60% DD
            (49_400.0, 0.50),  # 1.20% DD
            (49_200.0, 0.40),  # 1.60% DD
        )
        for equity, expected in cases:
            with self.subTest(equity=equity):
                self.assertEqual(
                    dynamic_risk.decide(self.settings, self.state, equity).risk_percent,
                    expected,
                )

    def test_risk_increases_again_when_equity_recovers(self):
        low = dynamic_risk.decide(self.settings, self.state, 49_200.0)
        recovered = dynamic_risk.decide(self.settings, self.state, 49_900.0)
        self.assertEqual(low.risk_percent, 0.40)
        self.assertEqual(recovered.risk_percent, 1.00)

    def test_fitting_tiers_never_invents_a_risk_value(self):
        self.assertEqual(
            dynamic_risk.fitting_tiers(self.settings, 0.80),
            (0.75, 0.50, 0.40),
        )

    def test_disabled_dynamic_risk_keeps_the_static_setting(self):
        settings = Settings(risk_percent=0.40, dynamic_risk_enabled=False)
        self.state.balance_high_water = 60_000.0
        self.assertEqual(
            dynamic_risk.decide(settings, self.state, 45_000.0).risk_percent,
            0.40,
        )

    def test_balance_high_water_survives_restart(self):
        self.assertTrue(self.state.observe_balance(50_500.0))
        self.assertFalse(self.state.observe_balance(50_400.0))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            self.state.save(path)
            restored = BotState.load(path)
        self.assertEqual(restored.balance_high_water, 50_500.0)
        self.assertEqual(
            dynamic_risk.decide(self.settings, restored, 50_000.0).risk_percent,
            0.50,
        )

    def test_dynamic_caps_must_hold_the_maximum_tier(self):
        with self.assertRaisesRegex(ValueError, "maximum per-setup risk"):
            dynamic_settings(max_open_risk_percent=0.80)

    def test_non_finite_risk_settings_fail_closed(self):
        for field, value in (("risk_percent", float("nan")),
                             ("max_open_risk_percent", float("inf")),
                             ("dynamic_risk_dd2_percent", float("nan"))):
            with self.subTest(field=field), self.assertRaisesRegex(
                    ValueError, "risk settings must be finite"):
                dynamic_settings(**{field: value})


class ProjectedExposureTests(unittest.TestCase):
    def setUp(self):
        self.settings = dynamic_settings(internal_daily_stop_percent=1.50)
        self.state = BotState(initial_balance=50_000.0)
        self.state.day_start_balance = 50_000.0
        self.state.day_start_equity = 50_000.0

    def test_pending_orders_are_counted_as_live_stop_risk(self):
        orders = [
            {"price": 4_000.0, "stop": 3_990.0, "volume": 0.50},
            {"price": 4_010.0, "stop": 4_020.0, "volume": 0.25},
        ]
        self.assertAlmostEqual(
            pending_risk_percent(GOLD, orders, 50_000.0), 1.50, places=8)

    def test_pending_risk_can_be_filtered_by_idea_direction(self):
        orders = [
            {"type_name": "BUY_LIMIT", "price": 4_000.0, "stop": 3_990.0,
             "volume": 0.25},
            {"type_name": "SELL_LIMIT", "price": 4_010.0, "stop": 4_020.0,
             "volume": 0.50},
        ]
        self.assertAlmostEqual(
            pending_risk_percent(GOLD, orders, 50_000.0, direction=1),
            0.50, places=8)
        self.assertAlmostEqual(
            pending_risk_percent(GOLD, orders, 50_000.0, direction=-1),
            1.00, places=8)

    def test_same_direction_pending_counts_toward_per_idea_cap(self):
        orders = [
            {"type_name": "BUY_LIMIT", "price": 4_000.0, "stop": 3_990.0,
             "volume": 0.50},
        ]
        verdict = guardrails.risk_per_idea(
            self.settings, GOLD, [], direction=1, balance=50_000.0,
            proposed_risk=0.75, orders=orders)
        self.assertFalse(verdict)

    def test_one_percent_setup_fits_but_a_second_one_is_blocked(self):
        first = guardrails.projected_internal_daily_risk(
            self.settings, self.state, 50_000.0, 50_000.0, 0.0, 1.0)
        second = guardrails.projected_internal_daily_risk(
            self.settings, self.state, 50_000.0, 50_000.0, 1.0, 1.0)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertIn("2.00% exceeds remaining internal daily room 1.50%",
                      second.reason)

    def test_loss_already_taken_reduces_the_remaining_daily_budget(self):
        verdict = guardrails.projected_internal_daily_risk(
            self.settings, self.state, 49_750.0, 50_000.0, 0.50, 0.75)
        self.assertFalse(verdict)  # 0.50% used + 0.50% open + 0.75% next

    def test_lower_day_reference_still_reserves_fixed_initial_capital_amount(self):
        self.state.day_start_balance = 49_000.0
        self.state.day_start_equity = 49_000.0
        verdict = guardrails.projected_internal_daily_risk(
            self.settings, self.state, 49_000.0, 50_000.0, 0.73, 0.75)
        # The configured 1.5% is always $750 of initial capital, not 1.5% of
        # the lower midnight balance. $740 remains after reserving 1.48%.
        self.assertTrue(verdict)

    def test_higher_day_reference_uses_its_actual_cash_room(self):
        self.state.day_start_balance = 51_000.0
        self.state.day_start_equity = 51_000.0
        verdict = guardrails.projected_internal_daily_risk(
            self.settings, self.state, 51_000.0, 50_000.0, 0.75, 0.75)
        # The daily room remains exactly $750 = 1.50% of initial capital.
        self.assertTrue(verdict)

    def test_projected_max_loss_blocks_a_combined_stop_below_the_static_floor(self):
        verdict = guardrails.projected_max_loss_risk(
            self.settings, self.state, initial_balance=50_000.0,
            open_risk=0.40, proposed_risk=0.40, balance=45_250.0,
        )
        self.assertFalse(verdict)
        self.assertIn("0.80% exceeds remaining maximum-loss room 0.50%",
                      verdict.reason)

    def test_projected_max_loss_allows_a_stop_that_stays_above_the_floor(self):
        verdict = guardrails.projected_max_loss_risk(
            self.settings, self.state, initial_balance=50_000.0,
            open_risk=0.0, proposed_risk=0.40, balance=45_250.0,
        )
        self.assertTrue(verdict)

    def test_unrealised_profit_cannot_fund_extra_daily_risk(self):
        verdict = guardrails.projected_internal_daily_risk(
            self.settings, self.state,
            equity=50_000.0, initial_balance=50_000.0,
            open_risk=0.25, proposed_risk=0.50,
            balance=49_500.0,
        )
        # Closed balance has only $250 (0.50%) above the internal floor. The
        # $500 floating profit is not durable room because it vanishes at SL.
        self.assertFalse(verdict)
        self.assertIn("0.75% exceeds remaining internal daily room 0.50%",
                      verdict.reason)

    def test_unrealised_loss_is_not_counted_twice(self):
        verdict = guardrails.projected_internal_daily_risk(
            self.settings, self.state,
            equity=49_750.0, initial_balance=50_000.0,
            open_risk=0.50, proposed_risk=0.50,
            balance=50_000.0,
        )
        # At both stops the closed account would be $49,500, still $250 above
        # the $49,250 internal floor. The $250 floating loss is already part of
        # the existing position's entry-to-stop risk and must not be subtracted.
        self.assertTrue(verdict)

    def test_actual_dynamic_tier_is_used_for_plan_sizing(self):
        broker = SimpleNamespace(spec=GOLD)
        intent = SimpleNamespace(risk=10.0, converted=False, action="limit")
        proposed = run._proposed_risk_percent(
            broker, self.settings, intent, 50_000.0, risk_percent=1.0)
        # $500 / ($10 * $100/point) = 0.50 lot = exactly 1%.
        self.assertAlmostEqual(proposed, 1.0, places=8)

    def test_second_setup_steps_down_to_a_configured_tier_that_fits(self):
        settings = dynamic_settings(dynamic_risk_fit_remaining=True)
        broker = SimpleNamespace(spec=GOLD)
        intent = SimpleNamespace(
            risk=10.0, converted=False, action="limit", direction=1,
            plan_id="M30@test")
        orders = [{
            "type_name": "BUY_LIMIT", "price": 4_000.0, "stop": 3_990.0,
            "volume": 0.51,
        }]
        selected, actual = run._fit_dynamic_setup_risk(
            broker, settings, self.state, intent, 50_000.0,
            equity=50_000.0, balance=50_000.0,
            positions=[], orders=orders, live_risk=1.02,
            requested_risk=1.00)
        self.assertEqual(selected, 0.40)
        self.assertAlmostEqual(actual, 0.40, places=8)
        self.assertLessEqual(1.02 + actual, 1.50)

    def test_fit_does_not_bypass_tighter_remaining_daily_room(self):
        settings = dynamic_settings(dynamic_risk_fit_remaining=True)
        broker = SimpleNamespace(spec=GOLD)
        intent = SimpleNamespace(
            risk=10.0, converted=False, action="limit", direction=1,
            plan_id="M30@test")
        orders = [{
            "type_name": "BUY_LIMIT", "price": 4_000.0, "stop": 3_990.0,
            "volume": 0.25,
        }]
        # Closed balance $49,600 leaves only $350 = 0.70% above the $49,250
        # daily floor. Existing 0.50% + the smallest 0.40% tier cannot fit.
        selected, actual = run._fit_dynamic_setup_risk(
            broker, settings, self.state, intent, 50_000.0,
            equity=49_600.0, balance=49_600.0,
            positions=[], orders=orders, live_risk=0.50,
            requested_risk=1.00)
        self.assertEqual(selected, 1.00)
        self.assertAlmostEqual(actual, 1.00, places=8)
        verdict = guardrails.projected_internal_daily_risk(
            settings, self.state, 49_600.0, 50_000.0, 0.50, actual,
            balance=49_600.0)
        self.assertFalse(verdict)

    def test_status_capacity_reports_a_fitting_lower_tier(self):
        settings = dynamic_settings(dynamic_risk_fit_remaining=True)
        orders = [{
            "ticket": 1, "type_name": "BUY_LIMIT", "price": 4_000.0,
            "stop": 3_990.0, "volume": 0.51,
        }]
        allowed, message = run.entry_capacity(
            settings, self.state, [], orders, GOLD, 50_000.0, requests=0,
            setup_risk=1.00, equity=50_000.0, balance=50_000.0)
        self.assertTrue(allowed)
        self.assertIn("next fit 0.40% nominal", message)


if __name__ == "__main__":
    unittest.main()
