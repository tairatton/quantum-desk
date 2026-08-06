"""The futures instance: contract sizing, leg splitting and TopStep's rules.

These are the parts where futures differ from forex in a way that costs money.
Sizing is tested for refusing rather than rounding up, splitting for never
inventing a leg it cannot fill, and the max loss floor for trailing the
end-of-day high water mark the way TopStep does and FTMO does not.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import guardrails, terminal, trader            # noqa: E402
from bot.broker import OrderRejected, ProjectXError, size_contracts    # noqa: E402
from bot.settings import Settings                       # noqa: E402
from engine.signals import Intent                              # noqa: E402
from engine.state import BotState                              # noqa: E402


def settings(**overrides) -> Settings:
    """A synthetic contract with round numbers: $2.00 per index point.

    `max_contracts` is a derived property now -- the cap depends on whether the
    root is a micro -- so tests that want a specific cap set the underlying
    `max_micro_contracts` / `max_mini_contracts`. The ladder is off unless a
    test asks for it, so the sizing tests stay about sizing.
    """
    base = dict(risk_dollars=200.0, max_open_risk_dollars=400.0,
                tick_size=0.25, tick_value=0.5,
                max_micro_contracts=10, max_mini_contracts=10,
                dynamic_risk_enabled=False,
                initial_balance=50_000.0)
    base.update(overrides)
    return Settings(**base)


def anchored_state(initial: float = 50_000.0, **overrides) -> BotState:
    """A state that has been through `anchor_initial_balance`, as live code is."""
    state = BotState()
    state.anchor_initial_balance(initial)
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


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
        self.assertEqual(size_contracts(settings(max_micro_contracts=3, max_mini_contracts=3), 10_000.0, 50.0), 3)

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
        base = dict(dynamic_risk_enabled=True, max_open_risk_dollars=800.0)
        base.update(overrides)
        return settings(**base)

    def state(self, high_water: float) -> BotState:
        state = BotState(initial_balance=50_000.0)
        state.balance_high_water = high_water
        return state

    def test_full_size_while_close_to_the_high_water_mark(self):
        risk = trader.risk_for(self.ladder(), self.state(50_000.0), equity=49_900.0)
        # Bounded by the $400 internal daily stop, not by the forex 1.00% ratio.
        self.assertAlmostEqual(risk, 400.0)

    def test_it_steps_down_as_drawdown_deepens(self):
        ladder, state = self.ladder(), self.state(50_000.0)
        self.assertAlmostEqual(trader.risk_for(ladder, state, 49_700.0), 300.0)
        self.assertAlmostEqual(trader.risk_for(ladder, state, 49_400.0), 250.0)
        self.assertAlmostEqual(trader.risk_for(ladder, state, 49_100.0), 200.0)

    def test_a_floating_profit_cannot_ratchet_the_high_water_mark(self):
        # Equity above the closed high water is still full size, but the mark
        # itself has not moved, so giving it back does not throttle the account.
        risk = trader.risk_for(self.ladder(), self.state(50_000.0), equity=51_000.0)
        self.assertAlmostEqual(risk, 400.0)

    def test_a_ladder_whose_top_tier_exceeds_the_exposure_cap_is_refused(self):
        with self.assertRaises(ValueError):
            settings(dynamic_risk_enabled=True, max_open_risk_dollars=300.0)

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
        capped = settings(max_micro_contracts=3, max_mini_contracts=3)
        self.assertFalse(guardrails.can_hold_contracts(capped, 4))
        self.assertTrue(guardrails.can_hold_contracts(capped, 3))

    def test_the_cap_counts_contracts_already_held(self):
        # Regression: the cap was checked one setup at a time, so two 50-lot
        # setups were each allowed and 100 micros reached the account.
        capped = settings(max_micro_contracts=50, max_mini_contracts=5)
        self.assertTrue(guardrails.can_hold_contracts(capped, 50))
        verdict = guardrails.can_hold_contracts(capped, 50, already_held=50)
        self.assertFalse(verdict)
        self.assertIn("already held", verdict.reason)

    def test_the_cap_follows_the_contract_class(self):
        # MGC is a micro: the applicable limit is 50, not the 5-mini number.
        micro = settings(contract_symbol="MGC")
        mini = settings(contract_symbol="ES")
        self.assertTrue(micro.is_micro)
        self.assertFalse(mini.is_micro)
        self.assertEqual(guardrails.contract_cap(settings(
            contract_symbol="MGC", max_micro_contracts=50, max_mini_contracts=5)), 50)
        self.assertEqual(guardrails.contract_cap(settings(
            contract_symbol="ES", max_micro_contracts=50, max_mini_contracts=5)), 5)

    def test_risk_that_does_not_fill_one_contract_is_refused(self):
        self.assertFalse(guardrails.can_hold_contracts(settings(), 0))

    def test_open_risk_is_measured_in_dollars(self):
        verdict = guardrails.can_open(settings(), anchored_state(),
                                      open_risk_dollars=300.0,
                                      open_count=1, pending_count=0,
                                      proposed_risk_dollars=200.0)
        self.assertFalse(verdict.allowed)         # 300 + 200 > 400

    def test_the_consistency_rule_warns_rather_than_halting(self):
        verdict = guardrails.consistency(settings(), best_day_profit=2_000.0,
                                         total_profit=3_000.0)
        self.assertFalse(verdict.allowed)
        self.assertFalse(verdict.fatal)

class AnchorAndTrailTests(unittest.TestCase):
    """The trailing floor may only ever rise. Regression for a floor that fell."""

    def test_an_unanchored_state_never_derives_a_floor_from_a_lower_balance(self):
        # Before: floor(50,000) was 48,000 and floor(49,000) was 47,000 on the
        # SAME state -- the account could lose 2,000, then 2,000 again, and
        # never breach, because the anchor followed the balance down.
        state = anchored_state(50_000.0)
        self.assertAlmostEqual(guardrails.max_loss_floor(settings(), state, 50_000), 48_000.0)
        self.assertAlmostEqual(guardrails.max_loss_floor(settings(), state, 49_000), 48_000.0)

    def test_trading_is_refused_until_the_initial_balance_is_anchored(self):
        verdict = guardrails.anchored(BotState())
        self.assertFalse(verdict)
        self.assertTrue(guardrails.anchored(anchored_state()))

    def test_unanchored_limits_never_use_the_current_balance_as_a_floor(self):
        state = BotState()
        self.assertEqual(guardrails.max_loss_floor(settings(), state, 50_000.0), 0.0)
        health = guardrails.account_health(settings(), state, 50_000.0, 50_000.0)
        self.assertFalse(health)
        self.assertIn("not anchored", health.reason)
        entry = guardrails.can_open(
            settings(), state, open_risk_dollars=0.0, open_count=0,
            pending_count=0, proposed_risk_dollars=200.0, equity=50_000.0)
        self.assertFalse(entry)
        self.assertIn("not anchored", entry.reason)
        self.assertEqual(guardrails.remaining_room(settings(), state, 50_000.0), 0.0)

    def test_anchoring_happens_once_and_is_not_overwritten(self):
        state = anchored_state(50_000.0)
        self.assertFalse(state.anchor_initial_balance(49_000.0))
        self.assertAlmostEqual(state.initial_balance, 50_000.0)

    def test_the_end_of_day_high_water_advances_on_roll_day(self):
        # Regression: nothing in production ever assigned eod_balance_high_water,
        # so the trailing floor sat frozen at the starting balance forever.
        state = anchored_state(50_000.0)
        state.roll_day(date(2026, 8, 4), 50_000.0)
        state.roll_day(date(2026, 8, 5), 51_500.0)
        self.assertAlmostEqual(state.eod_balance_high_water, 51_500.0)
        self.assertAlmostEqual(state.best_day_profit, 1_500.0)

    def test_a_losing_day_does_not_lower_the_high_water(self):
        state = anchored_state(50_000.0)
        state.roll_day(date(2026, 8, 4), 50_000.0)
        state.roll_day(date(2026, 8, 5), 51_500.0)
        state.roll_day(date(2026, 8, 6), 50_800.0)
        self.assertAlmostEqual(state.eod_balance_high_water, 51_500.0)
        self.assertAlmostEqual(state.worst_day_loss, -700.0)

    def test_an_intraday_spike_cannot_ratchet_the_floor(self):
        # TopStep trails the END-OF-DAY balance. A day that touches 52,000 and
        # closes at 50,100 must leave the floor where a 50,100 close puts it.
        state = anchored_state(50_000.0)
        state.roll_day(date(2026, 8, 4), 50_000.0)
        state.observe_balance(52_000.0)          # closed trade, mid-session
        state.roll_day(date(2026, 8, 5), 50_100.0)
        self.assertAlmostEqual(state.eod_balance_high_water, 50_100.0)
        self.assertAlmostEqual(guardrails.max_loss_floor(settings(), state), 48_100.0)

    def test_the_trailing_floor_still_freezes_at_the_starting_balance(self):
        state = anchored_state(50_000.0, eod_balance_high_water=53_000.0)
        self.assertAlmostEqual(guardrails.max_loss_floor(settings(), state), 50_000.0)


class RemainingRoomTests(unittest.TestCase):
    """An entry may never risk more than the account has left to lose."""

    def test_an_entry_larger_than_the_room_above_the_hard_floor_is_refused(self):
        # Equity 48,100 with a 48,000 floor leaves $100. A $200 entry whose stop
        # is hit ends the account.
        state = anchored_state(50_000.0)
        verdict = guardrails.can_open(settings(), state, open_risk_dollars=0.0,
                                      open_count=0, pending_count=0,
                                      proposed_risk_dollars=200.0, equity=48_100.0)
        self.assertFalse(verdict)
        self.assertIn("loss floor", verdict.reason)

    def test_the_daily_floor_binds_before_the_hard_floor(self):
        state = anchored_state(50_000.0, day_start_balance=50_000.0)
        # Internal stop is 400, so at 49,700 only 100 remains for today.
        verdict = guardrails.can_open(settings(), state, open_risk_dollars=0.0,
                                      open_count=0, pending_count=0,
                                      proposed_risk_dollars=200.0, equity=49_700.0)
        self.assertFalse(verdict)

    def test_open_risk_counts_against_the_same_room(self):
        state = anchored_state(50_000.0)
        verdict = guardrails.can_open(settings(), state, open_risk_dollars=250.0,
                                      open_count=1, pending_count=0,
                                      proposed_risk_dollars=200.0, equity=48_400.0)
        self.assertFalse(verdict)

    def test_a_trade_that_fits_the_room_is_allowed(self):
        state = anchored_state(50_000.0)
        self.assertTrue(guardrails.can_open(settings(), state, open_risk_dollars=0.0,
                                            open_count=0, pending_count=0,
                                            proposed_risk_dollars=200.0,
                                            equity=50_000.0))

    def test_equity_is_required_for_every_entry_decision(self):
        verdict = guardrails.can_open(
            settings(), anchored_state(), open_risk_dollars=0.0,
            open_count=0, pending_count=0, proposed_risk_dollars=200.0)
        self.assertFalse(verdict)
        self.assertIn("equity is required", verdict.reason)

    def test_the_ladder_fits_itself_into_the_room_left(self):
        from engine.dynamic_risk import fit_to_room

        ladder = settings(dynamic_risk_enabled=True, max_open_risk_dollars=800.0)
        self.assertAlmostEqual(fit_to_room(ladder, 400.0, 280.0), 250.0)
        self.assertAlmostEqual(fit_to_room(ladder, 400.0, 100.0), 100.0)
        self.assertAlmostEqual(fit_to_room(ladder, 400.0, 40.0), 0.0)
        self.assertAlmostEqual(
            trader.risk_for(ladder, anchored_state(), 50_000.0, room=280.0), 250.0)

    def test_fitting_tiers_uses_dollar_fields(self):
        # Regression: the copied helper read dynamic_risk_max_percent and
        # raised AttributeError the first time anything called it.
        from engine.dynamic_risk import fitting_tiers

        ladder = settings(dynamic_risk_enabled=True, max_open_risk_dollars=800.0)
        # Recovery tiers are part of the fit: they are the sizes that keep a
        # thin account trading at all.
        self.assertEqual(fitting_tiers(ladder, 400.0),
                         (400.0, 300.0, 250.0, 200.0, 100.0, 50.0))


class ConsistencyTargetTests(unittest.TestCase):
    """Breaking consistency raises the bar; it does not end the evaluation."""

    def test_the_target_rises_with_an_outsized_best_day(self):
        self.assertAlmostEqual(guardrails.required_target(settings(), 2_000.0), 4_000.0)
        self.assertAlmostEqual(guardrails.required_target(settings(), 1_000.0), 3_000.0)

    def test_progress_reports_the_raised_target_not_the_headline(self):
        # Regression: $3,000 of profit with a $2,000 best day needs $4,000, but
        # progress() declared the objectives met and the bot told the operator
        # to stop and submit an evaluation that had not passed.
        state = anchored_state(50_000.0, best_day_profit=2_000.0,
                               trading_days=["2026-08-04", "2026-08-05"])
        standing = guardrails.progress(settings(), state, 53_000.0)
        self.assertAlmostEqual(standing["target_dollars"], 4_000.0)
        self.assertTrue(standing["target_raised_by_consistency"])
        self.assertFalse(standing["objectives_met"])

    def test_account_health_keeps_trading_until_the_raised_target_is_met(self):
        state = anchored_state(50_000.0, best_day_profit=2_000.0,
                               trading_days=["2026-08-04", "2026-08-05"])
        self.assertTrue(guardrails.account_health(settings(), state, 53_000.0, 53_000.0))
        self.assertFalse(guardrails.account_health(settings(), state, 54_000.0, 54_000.0))

    def test_the_combine_has_no_minimum_trading_days(self):
        self.assertEqual(Settings().min_trading_days, 0)
        state = anchored_state(50_000.0)
        standing = guardrails.progress(settings(), state, 53_000.0)
        self.assertTrue(standing["objectives_met"])


class PartialEntryTests(unittest.TestCase):
    """An ambiguous gateway outcome must never look like "nothing happened"."""

    class FlakyBroker:
        def __init__(self, error, fail_after):
            self.error, self.fail_after, self.sent = error, fail_after, []

        def place_market(self, direction, contracts, stop_price, take_profit):
            if len(self.sent) >= self.fail_after:
                raise self.error
            self.sent.append(contracts)
            return {"orderId": len(self.sent)}

    def plan_settings(self) -> Settings:
        return settings(risk_dollars=300.0)

    def test_a_network_failure_after_an_accepted_leg_returns_a_result(self):
        # Regression: open_trade caught only OrderRejected while the real broker
        # raises ProjectXError for HTTP and network failures, so the exception
        # escaped and the caller never learned contracts were live.
        broker = self.FlakyBroker(ProjectXError("network timeout"), fail_after=1)
        result = trader.open_trade(broker, self.plan_settings(), intent())
        self.assertEqual(result.accepted_contracts, 1)
        self.assertTrue(result.ambiguous)
        self.assertTrue(result.needs_reconciliation)
        self.assertIn("timeout", result.error)

    def test_a_network_failure_on_the_first_leg_is_still_ambiguous(self):
        broker = self.FlakyBroker(ProjectXError("connection reset"), fail_after=0)
        result = trader.open_trade(broker, self.plan_settings(), intent())
        self.assertEqual(result.accepted_contracts, 0)
        self.assertTrue(result.needs_reconciliation)

    def test_accepted_order_ids_are_recorded_for_reconciliation(self):
        broker = self.FlakyBroker(ProjectXError("boom"), fail_after=2)
        result = trader.open_trade(broker, self.plan_settings(), intent())
        self.assertEqual(result.order_ids, [1, 2])

    def test_an_explicit_rejection_on_the_first_leg_still_raises(self):
        broker = self.FlakyBroker(OrderRejected("refused"), fail_after=0)
        with self.assertRaises(OrderRejected):
            trader.open_trade(broker, self.plan_settings(), intent())

    def test_acceptance_is_not_reported_as_a_fill(self):
        broker = self.FlakyBroker(ProjectXError("x"), fail_after=99)
        result = trader.open_trade(broker, self.plan_settings(), intent())
        self.assertEqual(result.accepted_contracts, 3)
        self.assertFalse(result.needs_reconciliation)
        self.assertEqual(result.order_ids, [1, 2, 3])

    def test_cost_points_are_configured_rather_than_zero(self):
        # A cost-covered breakeven needs a cost. Every call site passed 0.0
        # because no cost model existed.
        mgc = settings(tick_size=0.10, tick_value=1.00,
                       commission_per_contract=0.74, slippage_ticks=1.0)
        self.assertAlmostEqual(trader.cost_points(mgc), 0.174, places=3)
        self.assertGreater(
            trader.stop_after_tp1(intent(direction=1), 20_000.0,
                                  trader.cost_points(mgc)), 20_000.0)


class ProjectXIdentityTests(unittest.TestCase):
    """The futures state binds to a ProjectX account, not an MT5 login."""

    def test_a_projectx_account_object_binds(self):
        state = BotState()
        self.assertTrue(state.bind_account(
            {"id": 123, "name": "TopStep 50K", "balance": 50_000}))
        self.assertEqual(state.account_login, 123)
        self.assertEqual(state.account_server, "TopStep 50K")

    def test_a_different_account_is_refused(self):
        state = BotState()
        state.bind_account({"id": 123, "name": "TopStep 50K", "balance": 50_000})
        with self.assertRaises(ValueError):
            state.bind_account({"id": 999, "name": "Other", "balance": 50_000})

    def test_an_incomplete_identity_fails_closed(self):
        with self.assertRaises(ValueError):
            BotState().bind_account({"balance": 50_000})

    def test_the_trading_day_rolls_at_17_00_chicago(self):
        from engine.state import trading_day

        # 21:00 UTC on 4 Aug is 16:00 CT -- still the 4 Aug session.
        self.assertEqual(trading_day(datetime(2026, 8, 4, 21, 0, tzinfo=timezone.utc)),
                         date(2026, 8, 4))
        # 23:00 UTC is 18:00 CT, which already belongs to the 5 Aug session.
        self.assertEqual(trading_day(datetime(2026, 8, 4, 23, 0, tzinfo=timezone.utc)),
                         date(2026, 8, 5))


if __name__ == "__main__":
    unittest.main()


class LossRoomReserveTests(unittest.TestCase):
    """The buffer that turns "almost never breaches" into "does not breach"."""

    def test_the_reserve_is_subtracted_from_tradeable_room(self):
        state = anchored_state(50_000.0)
        raw = guardrails.remaining_room(settings(), state, 48_500.0,
                                        include_reserve=False)
        usable = guardrails.remaining_room(settings(), state, 48_500.0)
        self.assertAlmostEqual(raw, 500.0)
        self.assertAlmostEqual(usable, 500.0 - settings().loss_room_reserve_dollars)

    def test_a_trade_that_would_land_exactly_on_the_floor_is_refused(self):
        # $200 of room and a $200 trade: the stop lands the account ON the
        # floor, which is a breach, not a near miss.
        state = anchored_state(50_000.0)
        verdict = guardrails.can_open(settings(), state, open_risk_dollars=0.0,
                                      open_count=0, pending_count=0,
                                      proposed_risk_dollars=200.0, equity=48_200.0)
        self.assertFalse(verdict)

    def test_the_reserve_alone_leaves_no_tradeable_room(self):
        state = anchored_state(50_000.0)
        verdict = guardrails.can_open(settings(), state, open_risk_dollars=0.0,
                                      open_count=0, pending_count=0,
                                      proposed_risk_dollars=50.0, equity=48_150.0)
        self.assertFalse(verdict)
        self.assertIn("reserve", verdict.reason)

    def test_a_reserve_that_swallows_the_whole_max_loss_is_refused(self):
        # The reserve is measured against the account-ending floor now, so this
        # is the configuration that would leave no tradeable room at all.
        with self.assertRaises(ValueError):
            settings(loss_room_reserve_dollars=2_000.0)

    def test_a_reserve_larger_than_the_daily_stop_is_allowed(self):
        # It no longer applies to the daily floor, so it does not have to fit
        # inside it.
        self.assertTrue(settings(loss_room_reserve_dollars=500.0,
                                 internal_daily_stop_dollars=400.0))

    def test_the_reserve_is_configured_and_positive_in_production(self):
        from bot.settings import load

        self.assertGreater(load().loss_room_reserve_dollars, 0.0)


class LadderDepthTests(unittest.TestCase):
    """The ladder must keep stepping down where the floor is nearest."""

    def ladder(self) -> Settings:
        return settings(dynamic_risk_enabled=True, max_open_risk_dollars=800.0)

    def test_the_ladder_continues_past_the_floor_tier(self):
        from engine.dynamic_risk import ladder_steps

        steps = ladder_steps(self.ladder())
        sizes = [size for _, size in steps]
        self.assertEqual(sizes, [400.0, 300.0, 250.0, 200.0, 100.0, 50.0, 50.0])
        thresholds = [threshold for threshold, _ in steps]
        self.assertEqual(thresholds, sorted(thresholds))
        self.assertEqual(thresholds[-1], float("inf"))

    def test_the_terminal_tier_does_not_rebound_at_its_boundary(self):
        ladder, state = self.ladder(), anchored_state(50_000.0)
        self.assertAlmostEqual(trader.risk_for(ladder, state, 48_500.0), 50.0)

    def test_a_deep_drawdown_shrinks_the_trade_instead_of_holding_it_flat(self):
        # Regression: below the third threshold the ladder returned risk_dollars
        # forever, so an account $1,400 down requested the same size as one $760
        # down -- right where the trailing floor is closest.
        ladder, state = self.ladder(), anchored_state(50_000.0)
        state.balance_high_water = 50_000.0
        self.assertAlmostEqual(trader.risk_for(ladder, state, 49_100.0), 200.0)
        self.assertAlmostEqual(trader.risk_for(ladder, state, 48_900.0), 100.0)
        self.assertAlmostEqual(trader.risk_for(ladder, state, 48_600.0), 50.0)

    def test_recovery_tiers_let_a_thin_account_keep_trading(self):
        from engine.dynamic_risk import fit_to_room

        # $120 of room fits no ordinary tier, but $100 is reachable. Without it
        # the account is alive, unbreached and permanently unable to trade.
        self.assertAlmostEqual(fit_to_room(self.ladder(), 500.0, 120.0), 100.0)
        self.assertAlmostEqual(fit_to_room(self.ladder(), 500.0, 60.0), 50.0)
        self.assertAlmostEqual(fit_to_room(self.ladder(), 500.0, 40.0), 0.0)

    def test_recovery_tiers_must_be_smaller_than_the_floor_tier(self):
        with self.assertRaises(ValueError):
            settings(recovery_risk_dollars=(300.0,))

    def test_cost_aware_stops_reject_invalid_costs(self):
        with self.assertRaises(ValueError):
            trader.stop_after_tp1(intent(direction=1), 20_000.0, -1.0)
        with self.assertRaises(ValueError):
            trader.stop_after_tp2(intent(direction=1), float("nan"))


class ReserveScopeTests(unittest.TestCase):
    """The reserve guards the account-ending floor, not the daily lockout."""

    def test_the_daily_stop_is_not_reduced_by_the_reserve(self):
        # Regression: reserving against the daily floor too capped every trade
        # at internal_stop - reserve = $200, so the ladder's larger tiers could
        # never be reached and the evaluation took twice as long for protection
        # it already had.
        state = anchored_state(50_000.0, day_start_balance=50_000.0)
        room = guardrails.remaining_room(settings(), state, 50_000.0)
        self.assertAlmostEqual(room, settings().internal_daily_stop_dollars)

    def test_the_fatal_floor_still_keeps_its_reserve(self):
        state = anchored_state(50_000.0)
        room = guardrails.remaining_room(settings(), state, 48_300.0)
        self.assertAlmostEqual(room, 300.0 - settings().loss_room_reserve_dollars)

    def test_a_trade_landing_on_the_fatal_floor_is_still_refused(self):
        state = anchored_state(50_000.0)
        self.assertFalse(guardrails.can_open(
            settings(), state, open_risk_dollars=0.0, open_count=0,
            pending_count=0, proposed_risk_dollars=200.0, equity=48_200.0))


class DailyLossLimitTests(unittest.TestCase):
    """A day may never lose more than the firm allows, by construction."""

    def test_the_top_tier_fits_inside_the_internal_daily_stop(self):
        # Regression: a $500 top tier under a $400 daily stop is a size the
        # first trade of a day can never take -- the room fit dropped it to
        # $375 every time, so the ladder advertised a number it never used.
        s = Settings()
        self.assertLessEqual(s.dynamic_risk_max_dollars, s.internal_daily_stop_dollars)

    def test_a_top_tier_over_the_daily_stop_is_refused(self):
        with self.assertRaises(ValueError):
            settings(dynamic_risk_enabled=True, dynamic_risk_max_dollars=500.0,
                     internal_daily_stop_dollars=400.0,
                     max_open_risk_dollars=800.0)

    def test_total_open_risk_stays_under_the_firm_daily_limit(self):
        # Two concurrent stops are one day's loss, so the exposure cap has to
        # sit under the $1,000 the firm allows, not on it.
        s = Settings()
        self.assertLess(s.max_open_risk_dollars, s.daily_loss_limit_dollars)
        with self.assertRaises(ValueError):
            Settings(max_open_risk_dollars=1_000.0, daily_loss_limit_dollars=1_000.0)

    def test_a_days_room_never_exceeds_the_internal_stop(self):
        state = anchored_state(50_000.0, day_start_balance=50_000.0)
        room = guardrails.remaining_room(Settings(), state, 50_000.0)
        self.assertLessEqual(room, Settings().internal_daily_stop_dollars)
        self.assertLess(room, Settings().daily_loss_limit_dollars)
