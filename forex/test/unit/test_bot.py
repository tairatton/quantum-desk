"""Tests for the live layer: sizing arithmetic, guardrails and plan intents.

Nothing here touches MetaTrader. The broker is represented by its spec dataclass
and by small stand-ins, because the parts worth testing are the ones that decide
how much to risk and whether a trade is allowed at all.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from unittest import mock
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import guardrails
from engine import signals  # noqa: E402
from bot.broker import SymbolSpec  # noqa: E402
from bot.settings import Settings  # noqa: E402
from engine.sizing import SizingError, floor_to_step, open_risk_percent, size_plan  # noqa: E402
from engine.state import BotState, ManagedTrade  # noqa: E402

# XAUUSD on a typical retail account: 100 oz per lot, so 1.0 price point = $100.
GOLD = SymbolSpec(name="XAUUSDm", digits=3, point=0.001, volume_min=0.01,
                  volume_max=50.0, volume_step=0.01, value_per_point=100.0,
                  stops_level_points=0.0, filling=0)


@dataclass(frozen=True)
class FakePosition:
    direction: int
    volume: float
    price_open: float
    stop: float


class SizingTests(unittest.TestCase):
    def test_lots_match_the_requested_cash_risk(self):
        sizing = size_plan(GOLD, balance=100_000, risk_percent=0.40, stop_distance=16.0)
        self.assertEqual(sizing.risk_cash, 400.0)
        # 400 / (16 * 100) = 0.25 lots
        self.assertAlmostEqual(sizing.total_lots, 0.25, places=6)
        self.assertAlmostEqual(sum(sizing.legs), 0.25, places=6)

    def test_wider_stop_gives_smaller_size_for_the_same_risk(self):
        tight = size_plan(GOLD, 100_000, 0.40, 8.0)
        wide = size_plan(GOLD, 100_000, 0.40, 24.0)
        self.assertGreater(tight.total_lots, wide.total_lots)
        # The budget asked for is identical; what the lots can actually lose is
        # not, because the lot step only rounds down. 400/(24*100) is 0.1667 lots
        # and only 0.16 can be sent, which risks 384.
        self.assertEqual(tight.intended_risk_cash, wide.intended_risk_cash)
        self.assertAlmostEqual(tight.risk_cash, 400.0, places=6)
        self.assertAlmostEqual(wide.risk_cash, 384.0, places=6)

    def test_risk_cash_prices_the_lots_actually_sent(self):
        """Was: `risk_cash` held the intended budget, so a size rounded down read
        as riskier than it was — a full stop-out scored -0.52R instead of -1.00R
        and every live expectancy came out about 1.9x too generous."""
        # 0.40% of 10,000 is 40, but 0.01 lot of gold on a 20.96 stop risks 20.96.
        sizing = size_plan(GOLD, 10_000, 0.40, 20.96, weights=(1.0,))
        self.assertEqual(sizing.legs, (0.01,))
        self.assertAlmostEqual(sizing.intended_risk_cash, 40.0, places=6)
        self.assertAlmostEqual(sizing.risk_cash, 20.96, places=6)
        # R must divide by what can really be lost, or the ratio is meaningless.
        self.assertAlmostEqual(-sizing.risk_cash / sizing.risk_cash, -1.0, places=9)
        self.assertLess(sizing.risk_shortfall, 0.55)

    def test_risk_shortfall_is_one_when_nothing_is_rounded_away(self):
        sizing = size_plan(GOLD, 100_000, 0.40, 16.0)
        self.assertAlmostEqual(sizing.risk_shortfall, 1.0, places=6)

    def test_nearest_rounding_claims_the_lot_step_when_it_is_nearly_free(self):
        """0.40% of 10,000 wants 0.0191 lots. Flooring gives 0.21% — half the
        budget thrown away. Rounding to 0.02 costs 5% over target."""
        down = size_plan(GOLD, 10_000, 0.40, 20.96, weights=(1.0,), rounding="down")
        near = size_plan(GOLD, 10_000, 0.40, 20.96, weights=(1.0,), rounding="nearest")
        self.assertEqual(down.legs, (0.01,))
        self.assertEqual(near.legs, (0.02,))
        self.assertAlmostEqual(down.risk_cash, 20.96, places=6)
        self.assertAlmostEqual(near.risk_cash, 41.92, places=6)
        self.assertLess(near.risk_shortfall, 1.10)      # only just over target

    def test_nearest_rounding_refuses_to_overshoot_on_a_small_balance(self):
        """At 2,700 the wanted size is 0.0052 lots, so half a step is most of the
        position: rounding up would trade 0.78% against a 0.40% setting. The cap
        has to stop that, which leaves nothing sendable."""
        with self.assertRaises(SizingError):
            size_plan(GOLD, 2_700, 0.40, 20.96, weights=(1.0,), rounding="nearest")

    def test_the_overshoot_cap_is_the_thing_doing_the_work(self):
        # Same balance, cap lifted far enough to allow the doubling: now it sizes.
        loose = size_plan(GOLD, 2_700, 0.40, 20.96, weights=(1.0,),
                          rounding="nearest", max_overshoot=1.0)
        self.assertEqual(loose.legs, (0.01,))
        self.assertGreater(loose.risk_cash / loose.intended_risk_cash, 1.9)

    def test_nearest_never_exceeds_the_broker_volume_cap(self):
        near = size_plan(GOLD, 100_000_000, 0.40, 16.0, weights=(1.0,),
                         rounding="nearest")
        self.assertLessEqual(near.total_lots, GOLD.volume_max)

    def test_legs_follow_the_33_33_34_split(self):
        sizing = size_plan(GOLD, 100_000, 0.40, 16.0)
        self.assertEqual(len(sizing.legs), 3)
        self.assertFalse(sizing.single_leg)
        self.assertAlmostEqual(sum(sizing.legs), sizing.total_lots, places=8)
        for leg in sizing.legs:
            self.assertLessEqual(abs(leg - sizing.total_lots / 3), GOLD.volume_step)

    def test_split_stays_balanced_at_awkward_lot_counts(self):
        """Nine steps must go 3/3/3, not 2/2/5 — the exit policy depends on it."""
        for balance, expected in ((25_000, (0.03, 0.03, 0.03)),
                                  (12_500, (0.01, 0.01, 0.02)),
                                  (100_000, (0.12, 0.12, 0.12))):
            sizing = size_plan(GOLD, balance, 0.40, 11.05)
            self.assertEqual(tuple(round(leg, 2) for leg in sizing.legs), expected,
                             msg=f"balance {balance}")

    def test_three_legs_appear_as_soon_as_the_minimum_allows(self):
        """0.03 lots is exactly three minimum legs and must not fall back."""
        sizing = size_plan(GOLD, 12_000, 0.40, 16.0)
        self.assertEqual(sizing.legs, (0.01, 0.01, 0.01))
        self.assertFalse(sizing.single_leg)

    def test_small_account_falls_back_to_one_leg_rather_than_dropping_targets(self):
        sizing = size_plan(GOLD, 5_000, 0.40, 16.0)   # 20 / 1600 = 0.0125 -> 0.01
        self.assertTrue(sizing.single_leg)
        self.assertEqual(sizing.legs, (0.01,))

    def test_size_below_broker_minimum_is_refused_not_rounded_up(self):
        with self.assertRaises(SizingError):
            size_plan(GOLD, 1_000, 0.40, 16.0)

    def test_size_never_exceeds_broker_volume_cap(self):
        sizing = size_plan(replace(GOLD, volume_max=0.10), 100_000, 0.40, 16.0)
        self.assertLessEqual(sizing.total_lots, 0.10)

    def test_floor_to_step_is_immune_to_binary_rounding(self):
        self.assertAlmostEqual(floor_to_step(0.03, 0.01), 0.03, places=8)
        self.assertAlmostEqual(floor_to_step(0.0299, 0.01), 0.02, places=8)

    def test_open_risk_counts_only_stops_that_can_still_lose(self):
        running = FakePosition(1, 0.10, 4000.0, 3984.0)      # 16 pts * 100 * 0.10 = $160
        at_breakeven = FakePosition(1, 0.10, 4000.0, 4000.0)  # nothing left to lose
        self.assertAlmostEqual(
            open_risk_percent(GOLD, [running, at_breakeven], 100_000), 0.16, places=6)

    def test_missing_stop_is_treated_as_unbounded_risk(self):
        naked = FakePosition(1, 0.10, 4000.0, 0.0)
        self.assertEqual(open_risk_percent(GOLD, [naked], 100_000), float("inf"))


class GuardrailTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.state = BotState(initial_balance=100_000, day_key="2026-07-27",
                              day_start_balance=100_000)

    def test_healthy_account_is_allowed(self):
        self.assertTrue(guardrails.account_health(self.settings, self.state,
                                                  equity=100_500, balance=100_000))

    def test_max_loss_breach_halts_forever(self):
        verdict = guardrails.account_health(self.settings, self.state,
                                           equity=89_000, balance=100_000)
        self.assertFalse(verdict)
        self.assertTrue(verdict.fatal)
        self.assertTrue(self.state.halted_forever)
        # A restart must not clear it.
        self.assertFalse(guardrails.account_health(self.settings, self.state,
                                                   equity=100_000, balance=100_000))

    def test_internal_daily_stop_fires_before_the_ftmo_limit(self):
        verdict = guardrails.account_health(self.settings, self.state,
                                           equity=98_400, balance=100_000)
        self.assertFalse(verdict)
        self.assertFalse(verdict.fatal)
        self.assertIn("internal daily stop", verdict.reason)
        self.assertTrue(self.state.is_paused_today)

    def test_loss_streak_pauses_the_day(self):
        self.state.consecutive_losses = 3
        verdict = guardrails.account_health(self.settings, self.state, 100_000, 100_000)
        self.assertFalse(verdict)
        self.assertTrue(self.state.is_paused_today)

    def test_new_day_clears_a_pause(self):
        self.state.pause_today()
        self.state.roll_day(date(2026, 7, 28), 100_000)
        self.assertFalse(self.state.is_paused_today)
        self.assertEqual(self.state.day_start_balance, 100_000)

    def test_open_risk_cap_blocks_a_third_trade(self):
        verdict = guardrails.can_open(self.settings, self.state, open_risk=0.60,
                                      open_count=1, pending_count=0)
        self.assertFalse(verdict)
        self.assertIn("open risk", verdict.reason)

    def test_concurrent_trade_cap_counts_pending_orders(self):
        verdict = guardrails.can_open(self.settings, self.state, open_risk=0.0,
                                      open_count=1, pending_count=1)
        self.assertFalse(verdict)

    def test_entry_is_skipped_when_price_ran_past_the_plan(self):
        far = guardrails.entry_price_acceptable(self.settings, 1, plan_entry=4000.0,
                                                plan_risk=16.0, live_price=4004.0)
        near = guardrails.entry_price_acceptable(self.settings, 1, plan_entry=4000.0,
                                                plan_risk=16.0, live_price=4001.0)
        self.assertFalse(far)      # 4.0 / 16 = 0.25R > 0.15R
        self.assertTrue(near)      # 1.0 / 16 = 0.06R

    def test_favourable_price_is_never_rejected(self):
        self.assertTrue(guardrails.entry_price_acceptable(
            self.settings, 1, 4000.0, 16.0, live_price=3990.0))

    def test_progress_reports_room_left_against_each_limit(self):
        report = guardrails.progress(self.settings, self.state, equity=103_000)
        self.assertAlmostEqual(report["gain_percent"], 3.0, places=2)
        self.assertAlmostEqual(report["target_progress"], 30.0, places=1)
        self.assertAlmostEqual(report["max_loss_room_percent"], 13.0, places=2)

    def test_daily_room_is_measured_against_fixed_initial_capital(self):
        self.state.day_start_balance = 60_000.0
        report = guardrails.progress(self.settings, self.state, equity=59_000.0)
        # Floor = 60,000 - 5,000; remaining $4,000 is 4% of initial.
        self.assertEqual(report["daily_room_percent"], 4.0)

    def test_target_alone_does_not_meet_the_objectives(self):
        """FTMO's 2-Step also needs four trading days."""
        hit_target = guardrails.progress(self.settings, self.state, equity=111_000)
        self.assertFalse(hit_target["objectives_met"])
        for day in ("2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30"):
            self.state.day_key = day
            self.state.count_trading_day()
        self.assertTrue(guardrails.progress(
            self.settings, self.state, equity=111_000)["objectives_met"])

    def test_hedging_the_same_instrument_is_refused(self):
        longs = [FakePosition(1, 0.10, 4000.0, 3984.0)]
        self.assertFalse(guardrails.no_opposing_position(self.settings, longs, -1))
        self.assertTrue(guardrails.no_opposing_position(self.settings, longs, 1))

    def test_risk_per_idea_lets_both_timeframes_in_at_the_default_cap(self):
        one_leg = [FakePosition(1, 0.10, 4000.0, 3960.0)]      # 0.40% of 100k
        self.assertTrue(guardrails.risk_per_idea(
            self.settings, GOLD, one_leg, 1, 100_000))         # 0.40 + 0.40 <= 0.80

    def test_risk_per_idea_blocks_a_third_same_direction_entry(self):
        from dataclasses import replace as dc_replace

        two_legs = [FakePosition(1, 0.10, 4000.0, 3960.0),
                    FakePosition(1, 0.10, 4000.0, 3960.0)]     # 0.80% already used
        self.assertFalse(guardrails.risk_per_idea(
            self.settings, GOLD, two_legs, 1, 100_000))
        # The opposite direction is a different idea, so this guard ignores it —
        # no_opposing_position is what refuses that case.
        self.assertTrue(guardrails.risk_per_idea(
            self.settings, GOLD, two_legs, -1, 100_000))
        # Tightening the cap to one position at a time works as documented.
        strict = dc_replace(self.settings, max_risk_per_idea_percent=0.40)
        self.assertFalse(guardrails.risk_per_idea(
            strict, GOLD, [FakePosition(1, 0.10, 4000.0, 3960.0)], 1, 100_000))

    def test_entry_window_closes_before_the_weekly_close(self):
        from datetime import datetime

        # The blackout is 3h and the close is 23:00 server, so 19:30 is the last
        # half hour that still allows an entry. Holidays are covered in
        # test_trader.MarketHoursTests.
        allowed = datetime(2026, 7, 31, 19, 30)    # Friday, 3.5h before the close
        blocked = datetime(2026, 7, 31, 21, 30)    # Friday, inside the 3h blackout
        weekend = datetime(2026, 8, 1, 10, 0)      # Saturday, market shut
        self.assertTrue(guardrails.entry_window_open(self.settings, allowed))
        self.assertFalse(guardrails.entry_window_open(self.settings, blocked))
        self.assertFalse(guardrails.entry_window_open(self.settings, weekend))

    def test_news_blackout_blocks_only_its_own_window(self):
        from datetime import datetime

        span = [(datetime(2026, 7, 30, 14, 28), datetime(2026, 7, 30, 14, 32))]
        self.assertFalse(guardrails.entry_window_open(
            self.settings, datetime(2026, 7, 30, 14, 31), span))
        self.assertTrue(guardrails.entry_window_open(
            self.settings, datetime(2026, 7, 30, 14, 40), span))
        self.assertTrue(guardrails.entry_window_open(
            self.settings, datetime(2026, 7, 30, 14, 20), span))

    def test_missing_calendar_blocks_entries_only_when_asked_to(self):
        from dataclasses import replace as dc_replace
        from datetime import datetime

        moment = datetime(2026, 7, 30, 12, 0)      # Thursday, nothing else in the way
        self.assertTrue(guardrails.entry_window_open(
            self.settings, moment, (), calendar_usable=False))
        strict = dc_replace(self.settings, news_require_calendar=True)
        verdict = guardrails.entry_window_open(strict, moment, (), calendar_usable=False)
        self.assertFalse(verdict)
        self.assertIn("calendar unavailable", verdict.reason)


class NewsTests(unittest.TestCase):
    FEED = [
        {"title": "Non-Farm Employment Change", "country": "USD", "impact": "High",
         "date": "2026-07-30T12:30:00+00:00"},
        {"title": "Retail Sales", "country": "USD", "impact": "Medium",
         "date": "2026-07-30T14:00:00+00:00"},
        {"title": "ECB Press Conference", "country": "EUR", "impact": "High",
         "date": "2026-07-30T13:00:00+00:00"},
    ]

    def setUp(self):
        from engine import news

        self.news = news
        self.settings = Settings()
        self.calendar = news.Calendar(tuple(news._parse(self.FEED)),
                                     None, "cache")

    def test_feed_rows_become_utc_events(self):
        self.assertEqual(len(self.calendar.events), 3)
        first = self.calendar.events[0]
        self.assertEqual(first.currency, "USD")
        self.assertEqual(first.impact, "high")
        self.assertEqual(first.at_utc.hour, 12)

    def test_only_high_impact_usd_events_are_relevant_by_default(self):
        picked = self.news.relevant(self.calendar, self.settings)
        self.assertEqual([event.title for event in picked],
                         ["Non-Farm Employment Change"])

    def test_widening_impact_and_currencies_picks_up_more(self):
        from dataclasses import replace as dc_replace

        settings = dc_replace(self.settings, news_min_impact="medium",
                              news_currencies=("USD", "EUR"))
        self.assertEqual(len(self.news.relevant(self.calendar, settings)), 3)

    def test_window_is_shifted_onto_the_server_clock(self):
        """A UTC release at 12:30 is 15:30 on a UTC+3 server.

        The window is asymmetric — 5 minutes before, 3 after — because gold's
        spread widens ahead of a release and has usually recovered afterwards.
        """
        spans = self.news.windows(self.settings, 3.0, self.calendar)
        self.assertEqual(len(spans), 1)
        start, end = spans[0]
        self.assertEqual((start.hour, start.minute), (15, 25))
        self.assertEqual((end.hour, end.minute), (15, 33))

    def test_the_blackout_covers_five_minutes_before_and_three_after(self):
        from datetime import datetime

        spans = self.news.windows(self.settings, 3.0, self.calendar)
        release = datetime(2026, 7, 30, 15, 30)     # 12:30 UTC on a UTC+3 server
        for offset, expected in ((-6, True), (-4, False), (2, False), (4, True)):
            moment = release + timedelta(minutes=offset)
            verdict = guardrails.entry_window_open(self.settings, moment, spans)
            self.assertEqual(bool(verdict), expected,
                             f"{offset:+d} min from the release should be "
                             f"{'open' if expected else 'blocked'}")

    def test_manual_entries_are_treated_as_server_time_already(self):
        from dataclasses import replace as dc_replace

        settings = dc_replace(self.settings, news_times=("2026-08-03 09:00",))
        spans = self.news.windows(settings, 3.0, self.calendar)
        manual_span = [span for span in spans if span[0].day == 3][0]
        self.assertEqual((manual_span[0].hour, manual_span[0].minute), (8, 55))
        self.assertEqual((manual_span[1].hour, manual_span[1].minute), (9, 3))

    def test_broken_rows_are_skipped_not_fatal(self):
        parsed = self.news._parse([{"title": "no date"}, {"date": "not-a-date"},
                                   self.FEED[0]])
        self.assertEqual(len(parsed), 1)

    def test_next_event_looks_forward_only(self):
        from datetime import datetime

        before = self.news.next_event(self.settings, datetime(2026, 7, 30, 10, 0), 0.0,
                                      self.calendar)
        after = self.news.next_event(self.settings, datetime(2026, 7, 31, 10, 0), 0.0,
                                     self.calendar)
        self.assertIsNotNone(before)
        self.assertEqual(before[0].title, "Non-Farm Employment Change")
        self.assertIsNone(after)

    def test_expired_cache_is_not_usable_when_network_refresh_fails(self):
        stale = self.news.Calendar(
            self.calendar.events, datetime.now() - timedelta(hours=24), "cache",
        )
        failed = self.news.Calendar((), None, "none", "network unavailable")
        with mock.patch.object(self.news, "from_cache", return_value=stale), \
                mock.patch.object(self.news, "fetch", return_value=failed):
            loaded = self.news.load(self.settings)

        self.assertEqual(loaded.source, "stale-cache")
        self.assertFalse(loaded.usable)
        self.assertTrue(loaded.events)


class StateTests(unittest.TestCase):
    def test_round_trip_keeps_managed_trades(self):
        import tempfile

        state = BotState(initial_balance=100_000)
        state.trades["M30@x"] = ManagedTrade(
            plan_id="M30@x", timeframe="M30", direction=1, entry=4000.0, stop=3984.0,
            risk=16.0, risk_cash=400.0, targets=[4016.0, 4024.0, 4032.0],
            legs=[0.08, 0.08, 0.09], position_tickets=[1, 2, 3],
            market_order_tickets=[201], tp1_market_order_ticket=201,
            tp1_position_ticket=1, tp1_pending_ticket=101)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state.save(path)
            restored = BotState.load(path)
        self.assertEqual(restored.initial_balance, 100_000)
        self.assertEqual(restored.trades["M30@x"].legs, [0.08, 0.08, 0.09])
        self.assertEqual(restored.trades["M30@x"].position_tickets, [1, 2, 3])
        self.assertEqual(restored.trades["M30@x"].market_order_tickets, [201])
        self.assertEqual(restored.trades["M30@x"].tp1_market_order_ticket, 201)
        self.assertEqual(restored.trades["M30@x"].tp1_position_ticket, 1)
        self.assertEqual(restored.trades["M30@x"].tp1_pending_ticket, 101)

    def test_load_reopens_a_closed_trade_with_unresolved_market_orders(self):
        import json
        import tempfile

        payload = {
            "trades": {
                "M15@lagged": {
                    "plan_id": "M15@lagged",
                    "timeframe": "M15",
                    "direction": 1,
                    "entry": 4000.0,
                    "stop": 3984.0,
                    "risk": 16.0,
                    "risk_cash": 400.0,
                    "targets": [4016.0, 4024.0, 4032.0],
                    "legs": [0.08, 0.08, 0.09],
                    "market_order_tickets": [201, 202, 203],
                    "closed": True,
                    "exit_mode": "be_33_33_34",
                },
            },
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            restored = BotState.load(path)

        trade = restored.trades["M15@lagged"]
        self.assertFalse(trade.closed)
        self.assertIn(trade, restored.open_trades())

    def test_seen_plan_ids_are_capped(self):
        state = BotState()
        for index in range(500):
            state.remember_plan(f"plan-{index}", keep=10)
        self.assertEqual(len(state.seen_plan_ids), 10)
        self.assertEqual(state.seen_plan_ids[-1], "plan-499")

    def test_state_binds_to_one_broker_account(self):
        state = BotState(initial_balance=50_000)
        account = {"login": 123, "server": "FTMO-Demo", "balance": 50_100}
        self.assertTrue(state.bind_account(account))
        self.assertFalse(state.bind_account(account))
        with self.assertRaisesRegex(ValueError, "state belongs to"):
            state.bind_account({"login": 456, "server": "FTMO-Demo", "balance": 50_000})

    def test_legacy_state_refuses_a_materially_different_balance(self):
        state = BotState(initial_balance=10_000)
        with self.assertRaisesRegex(ValueError, "archive state.json"):
            state.bind_account(
                {"login": 456, "server": "FTMO-Demo", "balance": 50_000})

    def test_atomic_save_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            BotState(initial_balance=50_000).save(path)
            self.assertTrue(path.exists())
            self.assertFalse(path.with_name("state.json.tmp").exists())
            self.assertEqual(BotState.load(path).initial_balance, 50_000)

    def test_atomic_save_retries_a_transient_windows_file_lock(self):
        from engine import state as state_module

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            real_replace = state_module.os.replace
            attempts = 0

            def temporarily_locked(source, destination):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise PermissionError(5, "Access is denied", str(destination))
                real_replace(source, destination)

            with mock.patch.object(state_module.os, "replace",
                                   side_effect=temporarily_locked), \
                    mock.patch.object(state_module.time, "sleep") as sleep:
                BotState(initial_balance=50_000).save(path)

            self.assertEqual(attempts, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertFalse(path.with_name("state.json.tmp").exists())
            self.assertEqual(BotState.load(path).initial_balance, 50_000)

    def test_dry_run_state_mutations_cannot_replace_production_state(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            live_state = BotState(initial_balance=50_000)
            live_state.save(path)

            rehearsal = BotState.load(path)
            rehearsal.disable_persistence()
            rehearsal.initial_balance = 1.0
            rehearsal.seen_plan_ids.append("dry-only")
            rehearsal.save(path)

            restored = BotState.load(path)
            self.assertEqual(restored.initial_balance, 50_000)
            self.assertNotIn("dry-only", restored.seen_plan_ids)

    def test_invalid_production_settings_fail_loudly(self):
        from dataclasses import replace

        with self.assertRaisesRegex(ValueError, "max_open_risk_percent"):
            replace(Settings(), risk_percent=1.0, max_open_risk_percent=0.5)
        with self.assertRaisesRegex(ValueError, "reconnect_max_seconds"):
            replace(Settings(), reconnect_initial_seconds=60, reconnect_max_seconds=10)

    def test_journal_ignores_only_a_torn_final_record(self):
        import tempfile

        from engine import journal

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "journal.jsonl"
            path.write_text(
                '{"event":"complete","r":1.0}\n{"event":"torn"',
                encoding="utf-8",
            )
            self.assertEqual(
                journal.read(path),
                [{"event": "complete", "r": 1.0}],
            )

            path.write_text(
                '{"event":"complete"}\nnot-json\n{"event":"later"}\n',
                encoding="utf-8",
            )
            with self.assertRaises(__import__("json").JSONDecodeError):
                journal.read(path)

    def test_disabled_journal_write_does_not_pollute_live_evidence(self):
        import tempfile

        from engine import journal

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "journal.jsonl"
            previous = journal.set_enabled(False)
            try:
                record = journal.write(path, "trade_closed", r=-1.0)
            finally:
                journal.set_enabled(previous)
            self.assertEqual(record["event"], "trade_closed")
            self.assertFalse(path.exists())

    def test_journal_summary_orders_closes_by_timestamp(self):
        import tempfile

        from engine import journal

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "journal.jsonl"
            path.write_text(
                '{"at":"2026-08-03T10:00:00","event":"trade_closed","r":-9.0}\n'
                '{"at":"2026-08-01T10:00:00","event":"trade_closed","r":5.0}\n'
                '{"at":"2026-08-04T10:00:00","event":"trade_closed","r":-9.0}\n'
                '{"at":"2026-08-02T10:00:00","event":"trade_closed","r":5.0}\n',
                encoding="utf-8",
            )
            stats = journal.summarise(path)
            self.assertEqual(stats["net_r"], -8.0)
            self.assertEqual(stats["max_drawdown_r"], 18.0)

    def test_status_exposes_account_state_mismatch(self):
        from bot.run import account_binding_status

        state = BotState(initial_balance=10_000)
        matches, message = account_binding_status(
            state, {"login": 456, "server": "FTMO-Demo", "balance": 50_000})
        self.assertFalse(matches)
        self.assertIn("MISMATCH", message)

    def test_status_rejects_netting_even_when_login_matches(self):
        from bot.run import account_binding_status

        state = BotState(account_login=456, account_server="FTMO-Demo",
                         initial_balance=50_000)
        matches, message = account_binding_status(state, {
            "login": 456, "server": "FTMO-Demo", "balance": 50_000,
            "margin_mode": 0, "is_hedging": False,
        })
        self.assertFalse(matches)
        self.assertIn("UNSUPPORTED", message)
        self.assertIn("hedging required", message)


class ClockTests(unittest.TestCase):
    """The server-to-UTC offset cannot be measured while the market is closed."""

    class FakeBroker:
        def __init__(self, measured):
            self.measured = measured

        def server_utc_offset(self):
            return self.measured

    def setUp(self):
        from bot import run

        self.run = run
        self.settings = Settings()

    def test_live_tick_is_measured_and_remembered(self):
        state = BotState()
        offset, source = self.run.resolve_offset(self.FakeBroker(3.0), state, self.settings)
        self.assertEqual((offset, source), (3.0, "measured"))
        self.assertEqual(state.server_utc_offset, 3.0)

    def test_stale_tick_reuses_the_remembered_offset(self):
        state = BotState(server_utc_offset=2.0)
        offset, source = self.run.resolve_offset(self.FakeBroker(None), state, self.settings)
        self.assertEqual((offset, source), (2.0, "remembered"))

    def test_first_run_over_a_weekend_uses_the_configured_fallback(self):
        state = BotState()
        offset, source = self.run.resolve_offset(self.FakeBroker(None), state, self.settings)
        self.assertEqual((offset, source),
                         (self.settings.fallback_server_utc_offset, "fallback"))


def synthetic_bars(count: int = 400, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 4000 * np.exp(np.cumsum(rng.normal(0.0002, 0.003, count)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    wick = np.abs(rng.normal(0, 0.002, count)) * close
    return pd.DataFrame({
        "time": pd.date_range("2026-01-01", periods=count, freq="30min"),
        "open": open_, "high": np.maximum(open_, close) + wick,
        "low": np.minimum(open_, close) - wick, "close": close,
        "tick_volume": rng.integers(500, 5000, count), "spread": 240,
        "real_volume": 0,
    })


class SignalTests(unittest.TestCase):
    def test_intent_is_one_of_the_known_actions(self):
        intent = signals.read(synthetic_bars(), "M30")
        if intent is None:
            self.skipTest("no plan on this synthetic series")
        self.assertIn(intent.action, {"market", "limit", "cancel", signals.WAIT})
        self.assertEqual(intent.plan_id, f"M30@{intent.signal_time}")

    def test_stop_sits_on_the_losing_side_of_entry(self):
        intent = signals.read(synthetic_bars(), "M30")
        if intent is None:
            self.skipTest("no plan on this synthetic series")
        if intent.direction == 1:
            self.assertLess(intent.stop, intent.entry)
            self.assertGreater(intent.targets[0], intent.entry)
        else:
            self.assertGreater(intent.stop, intent.entry)
            self.assertLess(intent.targets[0], intent.entry)
        self.assertAlmostEqual(intent.risk, abs(intent.entry - intent.stop), places=6)

    def test_targets_are_1r_1_5r_and_2r(self):
        intent = signals.read(synthetic_bars(), "M30")
        if intent is None:
            self.skipTest("no plan on this synthetic series")
        for multiple, price in zip((1.0, 1.5, 2.0), intent.targets):
            expected = intent.entry + intent.direction * intent.risk * multiple
            self.assertAlmostEqual(price, expected, places=6)

    def test_bars_since_counts_closed_bars(self):
        frame = synthetic_bars(50)
        self.assertEqual(signals.bars_since(frame, str(frame["time"].iloc[-1])), 0)
        self.assertEqual(signals.bars_since(frame, str(frame["time"].iloc[-6])), 5)


if __name__ == "__main__":
    unittest.main()
