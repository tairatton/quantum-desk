"""The futures instance: contract sizing, leg splitting and TopStep's rules.

These are the parts where futures differ from forex in a way that costs money.
Sizing is tested for refusing rather than rounding up, splitting for never
inventing a leg it cannot fill, and the max loss floor for trailing the
end-of-day high water mark the way TopStep does and FTMO does not.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import guardrails, terminal, trader            # noqa: E402
from bot.broker import OrderRejected, size_contracts    # noqa: E402
from bot.settings import Settings                       # noqa: E402
from engine.signals import Intent                              # noqa: E402
from engine.state import BotState                              # noqa: E402


def settings(**overrides) -> Settings:
    base = dict(risk_dollars=200.0, max_open_risk_dollars=400.0,
                tick_size=0.25, tick_value=0.5, max_contracts=10,
                initial_balance=50_000.0)
    base.update(overrides)
    return Settings(**base)


def intent(direction: int = 1, entry: float = 20_000.0, stop: float = 19_950.0) -> Intent:
    return Intent(action="market", plan_id="p1", timeframe="M15", direction=direction,
                  entry=entry, stop=stop, risk=abs(entry - stop),
                  targets=(20_050.0, 20_100.0, 20_150.0), status="ready",
                  signal_time="2026-08-06T10:00:00", bars_since_signal=0)


class SizingTests(unittest.TestCase):
    def test_value_per_point_comes_from_tick_size_and_value(self):
        # MNQ: $0.50 per 0.25 point = $2.00 per index point.
        self.assertAlmostEqual(settings().value_per_point, 2.0)

    def test_size_rounds_down_to_whole_contracts(self):
        # 50 points at $2 = $100 per contract; $200 buys exactly 2.
        self.assertEqual(size_contracts(settings(), 200.0, 50.0), 2)
        # 60 points = $120 per contract; $200 buys 1, not 1.67 rounded up.
        self.assertEqual(size_contracts(settings(), 200.0, 60.0), 1)

    def test_a_stop_too_wide_for_one_contract_sizes_to_zero(self):
        # 120 points = $240, more than the $200 allowed. Taking one anyway would
        # risk 20% more than the plan; the honest answer is no trade.
        self.assertEqual(size_contracts(settings(), 200.0, 120.0), 0)

    def test_size_is_capped_by_the_scaling_plan(self):
        self.assertEqual(size_contracts(settings(max_contracts=3), 10_000.0, 50.0), 3)

    def test_planning_refuses_rather_than_rounding_up(self):
        with self.assertRaises(OrderRejected):
            trader.plan_contracts(settings(), entry=20_000.0, stop=19_880.0)

    def test_plan_reports_the_risk_actually_taken(self):
        plan = trader.plan_contracts(settings(), entry=20_000.0, stop=19_940.0)
        self.assertEqual(plan.contracts, 1)              # $120 per contract
        self.assertAlmostEqual(plan.risk_dollars, 120.0)
        self.assertAlmostEqual(plan.requested_risk_dollars, 200.0)
        self.assertAlmostEqual(plan.rounding_gave_up, 80.0)


class SplitTests(unittest.TestCase):
    def test_three_contracts_split_one_each(self):
        self.assertEqual(trader.split_contracts(3, (0.33, 0.33, 0.34)), (1, 1, 1))

    def test_the_remainder_goes_to_the_runner(self):
        self.assertEqual(trader.split_contracts(4, (0.33, 0.33, 0.34)), (1, 1, 2))

    def test_a_split_stays_proportional_above_three_contracts(self):
        # Regression: truncating each leg made 6 contracts come out 1/1/4, a
        # 17/17/66 split masquerading as 33/33/34, with two thirds of the
        # position riding to TP3 instead of one third.
        self.assertEqual(trader.split_contracts(6, (0.33, 0.33, 0.34)), (2, 2, 2))
        self.assertEqual(trader.split_contracts(9, (0.33, 0.33, 0.34)), (3, 3, 3))

    def test_every_split_sums_to_the_position_and_fills_every_leg(self):
        for total in range(3, 51):
            legs = trader.split_contracts(total, (0.33, 0.33, 0.34))
            self.assertEqual(sum(legs), total, total)
            self.assertTrue(all(leg >= 1 for leg in legs), total)
            self.assertLessEqual(max(legs) - min(legs), 1 + total // 30, total)

    def test_a_split_that_cannot_fill_every_leg_is_refused(self):
        with self.assertRaises(ValueError):
            trader.split_contracts(2, (0.33, 0.33, 0.34))

    def test_below_three_contracts_the_exit_is_the_single_leg_policy(self):
        plan = trader.plan_contracts(settings(), entry=20_000.0, stop=19_950.0)
        self.assertEqual(plan.contracts, 2)
        self.assertEqual(plan.exit_mode, "fixed_tp3")
        self.assertEqual(plan.legs, (2,))

    def test_three_contracts_unlock_the_split_exit(self):
        plan = trader.plan_contracts(settings(risk_dollars=300.0),
                                     entry=20_000.0, stop=19_950.0)
        self.assertEqual(plan.contracts, 3)
        self.assertEqual(plan.exit_mode, "be_33_33_34")
        self.assertEqual(plan.legs, (1, 1, 1))

    def test_the_single_leg_exit_runs_to_the_furthest_target(self):
        self.assertEqual(trader.targets_for(intent(), "fixed_tp3"), (20_150.0,))

    def test_the_split_exit_sends_one_leg_to_each_target(self):
        self.assertEqual(trader.targets_for(intent(), "be_33_33_34"),
                         (20_050.0, 20_100.0, 20_150.0))


class OpenTradeTests(unittest.TestCase):
    class FakeBroker:
        def __init__(self, fail_after: int | None = None):
            self.sent = []
            self.fail_after = fail_after

        def place_market(self, direction, contracts, stop_price, take_profit):
            if self.fail_after is not None and len(self.sent) >= self.fail_after:
                raise OrderRejected("gateway said no")
            self.sent.append((direction, contracts, stop_price, take_profit))
            return {"orderId": len(self.sent)}

    def test_every_leg_is_sent_with_its_own_stop(self):
        broker = self.FakeBroker()
        result = trader.open_trade(broker, settings(risk_dollars=300.0), intent())
        self.assertEqual(len(broker.sent), 3)
        self.assertTrue(all(leg[2] == 19_950.0 for leg in broker.sent))
        self.assertEqual(result.sent_contracts, 3)
        self.assertFalse(result.partial)

    def test_a_rejection_midway_is_reported_as_partial_not_as_failure(self):
        broker = self.FakeBroker(fail_after=2)
        result = trader.open_trade(broker, settings(risk_dollars=300.0), intent())
        self.assertTrue(result.partial)
        self.assertEqual(result.sent_contracts, 2)

    def test_a_rejection_on_the_first_leg_raises(self):
        with self.assertRaises(OrderRejected):
            trader.open_trade(self.FakeBroker(fail_after=0),
                              settings(risk_dollars=300.0), intent())

    def test_the_survivor_steps_up_to_lock_tp1_after_tp2(self):
        # Second step of the same technique the forex bot runs. Without it the
        # futures account trades a different system from the measured one.
        self.assertAlmostEqual(
            trader.stop_after_tp2(intent(direction=1), 1.0), 20_051.0)
        self.assertAlmostEqual(
            trader.stop_after_tp2(intent(direction=-1), 1.0), 20_049.0)

    def test_breakeven_includes_cost_in_the_direction_of_the_trade(self):
        self.assertAlmostEqual(
            trader.stop_after_tp1(intent(direction=1), 20_000.0, 2.0), 20_002.0)
        self.assertAlmostEqual(
            trader.stop_after_tp1(intent(direction=-1), 20_000.0, 2.0), 19_998.0)


class DynamicRiskTests(unittest.TestCase):
    """The forex ladder, in dollars. Same thresholds, same high-water rule."""

    def ladder(self, **overrides) -> Settings:
        # The top tier is $500, so the exposure cap has to make room for it --
        # the same validation the forex settings apply, and worth asserting
        # rather than working around: a ladder whose top tier cannot be taken
        # is a ladder that lies about the risk it will use.
        base = dict(dynamic_risk_enabled=True, max_open_risk_dollars=1_000.0)
        base.update(overrides)
        return settings(**base)

    def state(self, high_water: float) -> BotState:
        state = BotState(initial_balance=50_000.0)
        state.balance_high_water = high_water
        return state

    def test_full_size_while_close_to_the_high_water_mark(self):
        risk = trader.risk_for(self.ladder(), self.state(50_000.0), equity=49_900.0)
        self.assertAlmostEqual(risk, 500.0)          # forex 1.00%

    def test_it_steps_down_as_drawdown_deepens(self):
        ladder, state = self.ladder(), self.state(50_000.0)
        self.assertAlmostEqual(trader.risk_for(ladder, state, 49_700.0), 375.0)
        self.assertAlmostEqual(trader.risk_for(ladder, state, 49_400.0), 250.0)
        self.assertAlmostEqual(trader.risk_for(ladder, state, 49_100.0), 200.0)

    def test_a_floating_profit_cannot_ratchet_the_high_water_mark(self):
        # Equity above the closed high water is still full size, but the mark
        # itself has not moved, so giving it back does not throttle the account.
        risk = trader.risk_for(self.ladder(), self.state(50_000.0), equity=51_000.0)
        self.assertAlmostEqual(risk, 500.0)

    def test_a_ladder_whose_top_tier_exceeds_the_exposure_cap_is_refused(self):
        with self.assertRaises(ValueError):
            settings(dynamic_risk_enabled=True, max_open_risk_dollars=400.0)

    def test_the_ladder_is_off_by_default(self):
        self.assertAlmostEqual(
            trader.risk_for(settings(), self.state(50_000.0), 49_000.0), 200.0)


class TrailingMaxLossTests(unittest.TestCase):
    def state(self, **overrides) -> BotState:
        state = BotState(initial_balance=50_000.0)
        for key, value in overrides.items():
            setattr(state, key, value)
        return state

    def test_the_floor_starts_at_the_limit_below_the_initial_balance(self):
        floor = guardrails.max_loss_floor(settings(), self.state())
        self.assertAlmostEqual(floor, 48_000.0)

    def test_the_floor_trails_the_end_of_day_high_water_mark(self):
        floor = guardrails.max_loss_floor(
            settings(), self.state(eod_balance_high_water=51_000.0))
        self.assertAlmostEqual(floor, 49_000.0)

    def test_trailing_stops_at_the_initial_balance(self):
        # +$3,000 would put a naive floor at $51,000; the rule freezes it at the
        # starting balance instead. Getting this wrong invents a limit TopStep
        # does not have and halts a perfectly good account.
        floor = guardrails.max_loss_floor(
            settings(), self.state(eod_balance_high_water=53_000.0))
        self.assertAlmostEqual(floor, 50_000.0)

    def test_a_static_account_does_not_trail_at_all(self):
        floor = guardrails.max_loss_floor(
            settings(trailing_max_loss=False),
            self.state(eod_balance_high_water=53_000.0))
        self.assertAlmostEqual(floor, 48_000.0)

    def test_giving_back_an_earlier_days_profit_can_breach_a_trailing_floor(self):
        # The case that has no FTMO equivalent: the account is still above its
        # starting balance and is already gone.
        state = self.state(eod_balance_high_water=51_500.0)
        verdict = guardrails.account_health(settings(), state, equity=49_400.0,
                                            balance=49_400.0)
        self.assertFalse(verdict.allowed)
        self.assertTrue(verdict.fatal)
        self.assertTrue(state.halted_forever)


class SessionTests(unittest.TestCase):
    """The exchange week, on the exchange clock. Dates below are 2026.

    Aug 7 is a Friday, Aug 8 Saturday, Aug 9 Sunday, Aug 10 Monday.
    """

    def test_entries_are_refused_inside_the_flat_by_window(self):
        verdict = guardrails.session_open(settings(), datetime(2026, 8, 6, 15, 30))
        self.assertFalse(verdict.allowed)
        self.assertIn("flat-by", verdict.reason)

    def test_entries_are_refused_during_the_maintenance_halt(self):
        self.assertFalse(guardrails.session_open(settings(),
                                                 datetime(2026, 8, 6, 16, 30)))

    def test_the_morning_session_is_open(self):
        self.assertTrue(guardrails.session_open(settings(),
                                                datetime(2026, 8, 6, 9, 30)))

    def test_the_evening_session_is_open(self):
        # Regression: a naive "past the deadline" test blocked everything after
        # 15:10, which threw away the whole overnight session the strategy
        # trades on M15 and M30.
        self.assertTrue(guardrails.session_open(settings(),
                                                datetime(2026, 8, 10, 18, 0)))

    def test_sunday_morning_is_shut(self):
        # Regression: only Saturday was checked, so Sunday morning read as a
        # normal session eight hours before the exchange opens.
        verdict = guardrails.session_open(settings(), datetime(2026, 8, 9, 9, 30))
        self.assertFalse(verdict.allowed)
        self.assertIn("Sunday", verdict.reason)

    def test_the_week_opens_sunday_evening(self):
        self.assertTrue(guardrails.session_open(settings(),
                                                datetime(2026, 8, 9, 18, 0)))

    def test_friday_evening_does_not_reopen(self):
        verdict = guardrails.session_open(settings(), datetime(2026, 8, 7, 18, 0))
        self.assertFalse(verdict.allowed)
        self.assertIn("week closed", verdict.reason)

    def test_saturday_is_shut(self):
        self.assertFalse(guardrails.session_open(settings(),
                                                 datetime(2026, 8, 8, 9, 30)))

    def test_an_aware_timestamp_is_converted_not_reinterpreted(self):
        # Bangkok 09:30 Monday is 21:30 Sunday in Chicago (UTC+7 vs UTC-5) -- an open
        # evening session. Passing local wall-clock time as if it were exchange
        # time answered about the wrong half of the day.
        bangkok = timezone(timedelta(hours=7))
        moment = datetime(2026, 8, 10, 9, 30, tzinfo=bangkok)
        self.assertEqual(guardrails.exchange_now(settings(), moment).hour, 21)
        self.assertTrue(guardrails.session_open(settings(), moment))


class ExposureTests(unittest.TestCase):
    def test_contract_cap_is_a_rule_breach_not_a_preference(self):
        self.assertFalse(guardrails.can_hold_contracts(settings(max_contracts=3), 4))
        self.assertTrue(guardrails.can_hold_contracts(settings(max_contracts=3), 3))

    def test_risk_that_does_not_fill_one_contract_is_refused(self):
        self.assertFalse(guardrails.can_hold_contracts(settings(), 0))

    def test_open_risk_is_measured_in_dollars(self):
        verdict = guardrails.can_open(settings(), BotState(), open_risk_dollars=300.0,
                                      open_count=1, pending_count=0,
                                      proposed_risk_dollars=200.0)
        self.assertFalse(verdict.allowed)         # 300 + 200 > 400

    def test_the_consistency_rule_warns_rather_than_halting(self):
        verdict = guardrails.consistency(settings(), best_day_profit=2_000.0,
                                         total_profit=3_000.0)
        self.assertFalse(verdict.allowed)
        self.assertFalse(verdict.fatal)


if __name__ == "__main__":
    unittest.main()


class TerminalTests(unittest.TestCase):
    """The screen has to render from a state file alone.

    A terminal that needs the network to draw anything is a terminal that is
    blank exactly when the connection is the problem being diagnosed.
    """

    def state(self, **overrides) -> BotState:
        state = BotState(initial_balance=50_000.0)
        for key, value in overrides.items():
            setattr(state, key, value)
        return state

    def test_the_account_panel_renders_without_a_connection(self):
        lines = terminal.account_panel(settings(), self.state(), None)
        self.assertTrue(any("no connection" in line for line in lines))
        self.assertTrue(any("48,000" in line for line in lines))   # the floor

    def test_the_floor_shown_is_the_trailed_one(self):
        lines = terminal.account_panel(
            settings(), self.state(eod_balance_high_water=51_500.0), 51_500.0)
        self.assertTrue(any("49,500" in line for line in lines))

    def mgc(self) -> Settings:
        """Real MGC economics: 0.10 tick, $1.00 a tick, so $10 an index point.

        The shared `settings()` helper uses round synthetic numbers; this panel
        is about what the operator will actually see, so it is worth pinning to
        the contract the bot is configured to trade.
        """
        return settings(tick_size=0.10, tick_value=1.00, max_contracts=5)

    def test_the_order_panel_names_the_stop_that_cannot_be_traded(self):
        # 40 points on MGC is $400 a contract against $200 of risk: no trade,
        # and the screen has to say so rather than showing a rounded-up 1.
        lines = terminal.order_panel(self.mgc(), self.state(), 50_000.0)
        self.assertTrue(any("no trade" in line for line in lines))

    def test_the_order_panel_reports_real_risk_after_rounding(self):
        lines = terminal.order_panel(self.mgc(), self.state(), 50_000.0)
        row = next(line for line in lines if "stop 20 pts" in line)
        self.assertIn("1 contract", row)
        self.assertIn("$200 real risk", row)

    def test_the_bar_is_plain_text_when_not_a_terminal(self):
        # stdout is captured under pytest, so no escape codes should appear.
        self.assertNotIn("\033", terminal.bar(500.0, 2_000.0))
        self.assertEqual(terminal.bar(0.0, 0.0).strip(), "")
