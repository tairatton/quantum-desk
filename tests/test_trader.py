"""Tests for the order layer — the part that was previously untested.

`bot/code/trader.py` is where a plan becomes real orders and where every managed exit
lives, so it is also where a mistake costs money. Nothing here touches
MetaTrader: `FakeBroker` records what would have been sent and can be told to
reject a specific leg, which is the failure that used to strand half a trade.

Each test names the defect it pins down, because these all started as real bugs.
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.code import guardrails, market_hours, run, trader  # noqa: E402
from bot.code.broker import SymbolSpec  # noqa: E402
from bot.code.settings import Settings  # noqa: E402
from bot.code.signals import Intent  # noqa: E402
from bot.code.state import BotState  # noqa: E402
from xau.mt5_source import MT5Error  # noqa: E402

GOLD = SymbolSpec(name="XAUUSDm", digits=3, point=0.001, volume_min=0.01,
                  volume_max=50.0, volume_step=0.01, value_per_point=100.0,
                  stops_level_points=0.0, filling=0)


@dataclass
class FakePos:
    ticket: int
    direction: int = 1
    volume: float = 0.01
    price_open: float = 4000.0
    stop: float = 3984.0
    take_profit: float = 4016.0
    profit: float = 0.0
    symbol: str = "XAUUSDm"
    comment: str = ""
    opened_at: datetime = datetime(2026, 7, 27, 12, 0)


@dataclass
class FakeBroker:
    """Records writes instead of sending them."""
    dry_run: bool = False
    spec: SymbolSpec = GOLD
    server_time: datetime = datetime(2026, 7, 27, 12, 0)
    _positions: list = field(default_factory=list)
    _orders: list = field(default_factory=list)
    deals: list = field(default_factory=list)
    filled_orders: dict = field(default_factory=dict)
    finished_orders: dict = field(default_factory=dict)
    reject_leg: int | None = None       # 1-based leg number to refuse
    sent: list = field(default_factory=list)
    closed: list = field(default_factory=list)
    stops_moved: list = field(default_factory=list)
    cancelled: list = field(default_factory=list)
    _next_ticket: int = 100

    def tick(self) -> dict:
        return {"bid": 4000.0, "ask": 4000.2, "spread": 0.2,
                "server_time": self.server_time}

    def positions(self):
        return list(self._positions)

    def pending_orders(self):
        return list(self._orders)

    def closed_deals(self, since):
        return list(self.deals)

    def filled_order_positions(self, tickets, since=None):
        return {ticket: self.filled_orders[ticket]
                for ticket in tickets if ticket in self.filled_orders}

    def finished_order_states(self, tickets, since=None):
        return {ticket: self.finished_orders[ticket]
                for ticket in tickets if ticket in self.finished_orders}

    def _ticket(self) -> int:
        self._next_ticket += 1
        return self._next_ticket

    def _guard(self, what: str) -> None:
        self.sent.append(what)
        if self.reject_leg is not None and len(self.sent) == self.reject_leg:
            raise MT5Error(f"{what} rejected: retcode=10016 Invalid stops")

    def market_entry(self, direction, volume, stop, take_profit, comment) -> dict:
        self._guard(f"market {volume}")
        ticket = self._ticket()
        self._positions.append(FakePos(ticket=ticket, direction=direction, volume=volume,
                                       stop=stop, take_profit=take_profit))
        return {"dry_run": False, "order": ticket, "deal": ticket}

    def limit_entry(self, direction, volume, price, stop, tp, expires_at, comment) -> dict:
        self._guard(f"limit {volume}")
        ticket = self._ticket()
        self._orders.append({"ticket": ticket, "type": 2, "price": price,
                             "volume": volume, "comment": comment})
        return {"dry_run": False, "order": ticket}

    def move_stop(self, position, stop) -> dict:
        self.stops_moved.append((position.ticket, stop))
        position.stop = stop
        return {"dry_run": False}

    def close(self, position, reason) -> dict:
        self.closed.append((position.ticket, reason))
        self._positions = [p for p in self._positions if p.ticket != position.ticket]
        return {"dry_run": False}

    def cancel(self, ticket, reason) -> dict:
        self.cancelled.append((ticket, reason))
        self._orders = [o for o in self._orders if o["ticket"] != ticket]
        self.finished_orders[ticket] = "CANCELED"
        return {"dry_run": False}

    # test helpers -------------------------------------------------------
    def fill_order(self, ticket: int) -> int:
        """Fill an order using a distinct MT5 position identifier."""
        order = next(o for o in self._orders if o["ticket"] == ticket)
        self._orders.remove(order)
        position_ticket = self._ticket()
        self._positions.append(FakePos(ticket=position_ticket, volume=order["volume"],
                                       comment=order["comment"]))
        self.filled_orders[ticket] = position_ticket
        self.finished_orders[ticket] = "FILLED"
        return position_ticket


def bars(count: int = 300, timeframe_minutes: int = 15) -> pd.DataFrame:
    start = pd.Timestamp("2026-06-01 00:00")
    return pd.DataFrame({
        "time": [start + pd.Timedelta(minutes=timeframe_minutes * i) for i in range(count)],
        "open": 4000.0, "high": 4001.0, "low": 3999.0, "close": 4000.0, "volume": 1,
    })


def intent(action: str = "market", timeframe: str = "M15") -> Intent:
    return Intent(action=action, plan_id=f"{timeframe}@2026-06-01 00:00:00",
                  timeframe=timeframe, direction=1, entry=4000.0, stop=3984.0,
                  risk=16.0, targets=(4016.0, 4024.0, 4032.0), status="BUY ACTIVE",
                  signal_time="2026-06-01 00:00:00", bars_since_signal=0)


class TraderPathsTests(unittest.TestCase):
    """Placing legs, including when the broker refuses one part-way through."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        # Redirect both files trader.py writes to, so tests touch no real state.
        self._patched = {}
        for module, name, value in ((trader, "JOURNAL_PATH", base / "journal.jsonl"),
                                    (trader, "STATE_PATH", base / "state.json")):
            self._patched[name] = getattr(module, name)
            setattr(module, name, value)
        # These exercise the three-leg machinery — break-even, per-leg tickets,
        # partial rejection — so they ask for it by name rather than leaning on
        # whatever the default happens to be. The default is `fixed_tp3`, which
        # is one leg and would make most of these vacuous.
        self.settings = replace(Settings(), exit_mode="be_33_33_34")
        self.state = BotState(initial_balance=100_000.0)
        self.state.roll_day(date(2026, 6, 1), 100_000.0, 100_000.0)

    def tearDown(self):
        for name, value in self._patched.items():
            setattr(trader, name, value)
        self.tmp.cleanup()

    def test_three_legs_are_placed_and_the_trade_is_recorded(self):
        broker = FakeBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        self.assertEqual(len(broker.sent), 3)
        self.assertEqual(len(trade.position_tickets), 3)
        self.assertFalse(trade.closed)
        self.assertIn(trade.plan_id, self.state.trades)
        self.assertEqual(self.state.trading_days, ["2026-06-01"])

    def test_a_rejected_second_leg_keeps_the_first_under_management(self):
        """Was: the trade was only recorded after all three legs succeeded, so a
        rejection left a live position no part of the bot knew about."""
        broker = FakeBroker(reject_leg=2)
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        self.assertEqual(len(broker._positions), 1)          # one leg really opened
        self.assertIn(trade.plan_id, self.state.trades)       # and the bot owns it
        self.assertEqual(trade.position_tickets,
                         [broker._positions[0].ticket])
        self.assertFalse(trade.closed)
        self.assertTrue(trade.fill_bar_time)                 # so it can still time out

    def test_a_rejected_first_limit_leg_does_not_let_the_plan_be_retried(self):
        """Was: a pending plan whose first leg failed looked unseen, so the next
        bar sent it again."""
        broker = FakeBroker(reject_leg=1)
        trade = trader.open_trade(broker, self.settings, self.state,
                                  intent("limit"), 100_000.0)
        self.assertEqual(broker._orders, [])
        self.assertIn(trade.plan_id, self.state.trades)
        self.assertTrue(trade.closed)          # nothing live, nothing to manage
        self.assertIn(trade.plan_id, self.state.seen_plan_ids)

    def test_a_sizing_refusal_sends_nothing(self):
        broker = FakeBroker()
        tiny = trader.open_trade(broker, self.settings, self.state, intent(), 100.0)
        self.assertIsNone(tiny)
        self.assertEqual(broker.sent, [])

    def test_pending_fill_stamps_the_bar_that_starts_the_timeout_clock(self):
        """Was: `fill_bar_time` was only set for market entries, so retracement
        entries never reached the 120-bar timeout."""
        broker = FakeBroker()
        trade = trader.open_trade(broker, self.settings, self.state,
                                  intent("limit"), 100_000.0)
        self.assertIsNone(trade.fill_bar_time)
        order_tickets = list(trade.pending_tickets)
        position_tickets = [broker.fill_order(ticket) for ticket in order_tickets]
        self.assertTrue(set(order_tickets).isdisjoint(position_tickets))
        frame = bars(300)
        trader.sync_fills(broker, self.state, {"M15": frame})
        self.assertEqual(trade.fill_bar_time, str(frame["time"].iloc[-1]))
        self.assertEqual(trade.position_tickets, position_tickets)
        self.assertEqual(self.state.trading_days, ["2026-06-01"])

    def test_missing_pending_order_waits_when_mt5_history_is_not_ready(self):
        broker = FakeBroker()
        trade = trader.open_trade(broker, self.settings, self.state,
                                  intent("limit"), 100_000.0)
        original = list(trade.pending_tickets)
        broker._orders = []
        trader.sync_fills(broker, self.state, {"M15": bars(300)})
        self.assertEqual(trade.pending_tickets, original)
        self.assertFalse(trade.closed)
    def test_a_limit_filled_trade_does_time_out_at_120_bars(self):
        broker = FakeBroker()
        trade = trader.open_trade(broker, self.settings, self.state,
                                  intent("limit"), 100_000.0)
        for ticket in list(trade.pending_tickets):
            broker.fill_order(ticket)
        early = bars(200)
        trader.sync_fills(broker, self.state, {"M15": early})
        # 100 bars later: still inside the window.
        trader.enforce_timeout(broker, self.state, {"M15": bars(300)})
        self.assertEqual(broker.closed, [])
        # 150 bars later: past it.
        trader.enforce_timeout(broker, self.state, {"M15": bars(350)})
        self.assertEqual(len(broker.closed), 3)
        self.assertTrue(all(reason == "timeout 120 bars" for _, reason in broker.closed))

    def test_timeout_does_not_fire_twice_for_the_same_trade(self):
        broker = FakeBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        trade.fill_bar_time = str(bars(1)["time"].iloc[0])
        trader.enforce_timeout(broker, self.state, {"M15": bars(350)})
        first = len(broker.closed)
        trader.enforce_timeout(broker, self.state, {"M15": bars(351)})
        self.assertEqual(len(broker.closed), first)

    def test_breakeven_moves_the_survivors_once_tp1_is_banked(self):
        broker = FakeBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        first, *rest = trade.position_tickets
        broker._positions = [p for p in broker._positions if p.ticket != first]
        trader.apply_breakeven(broker, self.state)
        self.assertEqual(sorted(ticket for ticket, _ in broker.stops_moved), sorted(rest))
        self.assertTrue(all(stop == trade.entry for _, stop in broker.stops_moved))
        self.assertTrue(trade.breakeven_done)

    def test_breakeven_does_nothing_when_the_stop_took_every_leg(self):
        broker = FakeBroker()
        trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        broker._positions = []          # SL hit: all three gone at once
        trader.apply_breakeven(broker, self.state)
        self.assertEqual(broker.stops_moved, [])

    def test_missing_deal_history_does_not_close_trade_at_zero(self):
        broker = FakeBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        broker._positions = []
        broker.deals = []
        finished = trader.reconcile_closed(broker, self.state)
        self.assertEqual(finished, [])
        self.assertFalse(trade.closed)
        self.assertEqual(self.state.day_realised, 0.0)
    def test_r_is_scored_after_commission_and_swap(self):
        """Was: only `deal.profit` counted, so every live R read better than the
        money in the account — the exact number used to judge the edge."""
        broker = FakeBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        tickets = list(trade.position_tickets)
        broker._positions = []
        broker.deals = [
            {"position": tickets[0], "profit": 400.0, "commission": -7.0, "swap": -3.0,
             "net": 390.0},
            {"position": tickets[1], "profit": 200.0, "commission": -7.0, "swap": -3.0,
             "net": 190.0},
            {"position": tickets[2], "profit": -200.0, "commission": -7.0, "swap": -3.0,
             "net": -210.0},
        ]
        finished = trader.reconcile_closed(broker, self.state)
        self.assertEqual(len(finished), 1)
        record = finished[0]
        self.assertAlmostEqual(record["profit"], 370.0)     # 400+200-200 less 30 of cost
        self.assertAlmostEqual(record["costs"], -30.0)
        self.assertAlmostEqual(record["r"], 370.0 / trade.risk_cash, places=4)
        self.assertEqual(self.state.consecutive_losses, 0)


class TerminalNotificationTests(unittest.TestCase):
    def test_exposure_lists_open_positions_and_pending_orders(self):
        positions = [FakePos(ticket=501, profit=12.5, comment="M15 TP1")]
        orders = [{"ticket": 601, "type_name": "BUY_LIMIT", "volume": 0.01,
                   "price": 3990.0, "stop": 3980.0, "take_profit": 4010.0,
                   "comment": "M15 TP2", "expires_at": None}]
        output = io.StringIO()
        with redirect_stdout(output):
            run.print_exposure(positions, orders)
        rendered = output.getvalue()
        self.assertIn("open_positions=1", rendered)
        self.assertIn("pending_orders=1", rendered)
        self.assertIn("ticket=501", rendered)
        self.assertIn("#601 BUY_LIMIT", rendered)

    def test_dashboard_rows_keep_a_stable_production_width(self):
        output = io.StringIO()
        with redirect_stdout(output):
            run._panel_title("QUANTUM DESK | EXECUTION MONITOR", "LIVE | READY")
            run._panel_section("Exposure")
            run._panel_row("Summary", "Positions 0    Pending 0")
            run._panel_border("=")
        rows = output.getvalue().splitlines()
        self.assertTrue(rows)
        self.assertTrue(all(len(row) == run.PANEL_WIDTH for row in rows))

    def test_heartbeat_countdown_is_fixed_width(self):
        self.assertEqual(run._countdown(332), "00:05:32")
        self.assertEqual(run._countdown(3661), "01:01:01")

    def test_redirected_console_output_does_not_contain_ansi_codes(self):
        output = io.StringIO()
        with redirect_stdout(output):
            run._panel_title("QUANTUM DESK | EXECUTION MONITOR", "LIVE | READY")
            print(run.status_line("HEARTBEAT", "healthy", "ok"))
        self.assertNotIn("\x1b[", output.getvalue())

    def test_untracked_broker_tickets_raise_terminal_alerts(self):
        output = io.StringIO()
        with redirect_stdout(output):
            run.print_management_alerts(
                BotState(), [FakePos(ticket=701)],
                [{"ticket": 702, "type_name": "BUY_LIMIT", "volume": 0.01,
                  "price": 3990.0, "comment": "orphan", "expires_at": None}])
        rendered = output.getvalue()
        self.assertIn("UNTRACKED_POSITION", rendered)
        self.assertIn("UNTRACKED_PENDING", rendered)

class DailyRulesTests(unittest.TestCase):
    """The day boundary, the loss streak and the daily-loss reference."""

    def setUp(self):
        self.settings = Settings()

    def test_a_loss_streak_clears_on_the_next_server_day(self):
        """Was: `roll_day` never reset the streak and only a win could clear it,
        so a day ending on the third loss paused the bot permanently."""
        state = BotState(initial_balance=100_000.0)
        state.roll_day(date(2026, 6, 1), 100_000.0, 100_000.0)
        state.consecutive_losses = 3
        self.assertFalse(guardrails.account_health(self.settings, state,
                                                   100_000.0, 100_000.0))
        state.roll_day(date(2026, 6, 2), 100_000.0, 100_000.0)
        self.assertEqual(state.consecutive_losses, 0)
        self.assertTrue(guardrails.account_health(self.settings, state,
                                                  100_000.0, 100_000.0))

    def test_daily_floor_uses_the_higher_of_balance_and_equity(self):
        """FTMO measures from whichever was higher at the day's open, and holding
        a winner overnight puts equity above balance."""
        state = BotState(initial_balance=100_000.0)
        state.roll_day(date(2026, 6, 1), 100_000.0, 104_000.0)
        self.assertEqual(state.day_start_equity, 104_000.0)
        # 5% of 104,000 is 98,800 — a floor 200 above the balance-only one.
        verdict = guardrails.account_health(self.settings, state, 98_700.0, 100_000.0)
        self.assertFalse(verdict)
        self.assertIn("daily loss limit", verdict.reason)

    def test_trading_stops_once_the_target_and_the_four_days_are_both_in(self):
        """Was: `progress()` computed `objectives_met` and nothing read it, so the
        bot kept trading after passing and could hand the pass back."""
        state = BotState(initial_balance=100_000.0)
        state.roll_day(date(2026, 6, 5), 110_500.0, 110_500.0)
        state.trading_days = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"]
        verdict = guardrails.account_health(self.settings, state, 110_500.0, 110_500.0)
        self.assertFalse(verdict)
        self.assertIn("objectives met", verdict.reason)
        self.assertFalse(verdict.fatal)      # a pass is not a failure

    def test_the_target_alone_does_not_stop_trading_before_day_four(self):
        state = BotState(initial_balance=100_000.0)
        state.roll_day(date(2026, 6, 2), 110_500.0, 110_500.0)
        state.trading_days = ["2026-06-01", "2026-06-02"]
        self.assertTrue(guardrails.account_health(self.settings, state,
                                                  110_500.0, 110_500.0))

    def test_the_request_allowance_stops_new_entries_near_the_cap(self):
        """Exceeding the EA request allowance is a forbidden practice, and the
        budget used to be assumed safe rather than counted."""
        state = BotState(initial_balance=100_000.0)
        self.assertTrue(guardrails.can_open(self.settings, state, 0.0, 0, 0, 100))
        near = int(self.settings.max_requests_per_day * 0.95)
        verdict = guardrails.can_open(self.settings, state, 0.0, 0, 0, near)
        self.assertFalse(verdict)
        self.assertIn("server requests", verdict.reason)

    def test_a_plan_the_account_cannot_margin_is_refused_before_any_order(self):
        account = {"margin_free": 1_000.0}
        self.assertTrue(guardrails.margin_available(account, 200.0, 3))
        verdict = guardrails.margin_available(account, 400.0, 3)
        self.assertFalse(verdict)
        self.assertIn("margin", verdict.reason)
        # An unanswerable query must not block: the broker decides in the end.
        self.assertTrue(guardrails.margin_available(account, None, 3))

    def test_state_survives_an_unknown_field_on_disk(self):
        import json

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text(json.dumps({
                "initial_balance": 25_000.0, "from_a_future_version": True,
                "trades": {"M15@x": {"plan_id": "M15@x", "timeframe": "M15",
                                     "direction": 1, "entry": 1.0, "stop": 0.5,
                                     "risk": 0.5, "risk_cash": 100.0,
                                     "targets": [1.5], "legs": [0.01],
                                     "unknown_leg_field": 7}}}), encoding="utf-8")
            state = BotState.load(path)
        self.assertEqual(state.initial_balance, 25_000.0)
        self.assertIn("M15@x", state.trades)


class ExitModeTests(unittest.TestCase):
    """The exit must be a setting, not a by-product of the account balance."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self._patched = {}
        for name, value in (("JOURNAL_PATH", base / "journal.jsonl"),
                            ("STATE_PATH", base / "state.json")):
            self._patched[name] = getattr(trader, name)
            setattr(trader, name, value)
        self.state = BotState(initial_balance=100_000.0)
        self.state.roll_day(date(2026, 6, 1), 100_000.0, 100_000.0)

    def tearDown(self):
        for name, value in self._patched.items():
            setattr(trader, name, value)
        self.tmp.cleanup()

    def test_fixed_tp3_stays_one_leg_however_large_the_account(self):
        """Was: `_split` gave three legs as soon as the size allowed, so growing
        past roughly 16,000 swapped the exit for a different system silently."""
        settings = replace(Settings(), exit_mode="fixed_tp3")
        for balance in (10_000, 25_000, 100_000):
            broker = FakeBroker()
            state = BotState(initial_balance=balance)
            state.roll_day(date(2026, 6, 1), balance, balance)
            trade = trader.open_trade(broker, settings, state, intent(), balance)
            self.assertEqual(len(trade.legs), 1, f"balance {balance} split the position")
            self.assertEqual(trade.targets, [intent().targets[2]],
                             f"balance {balance} did not exit at TP3")

    def test_be_33_33_34_really_places_three_legs(self):
        settings = replace(Settings(), exit_mode="be_33_33_34")
        broker = FakeBroker()
        trade = trader.open_trade(broker, settings, self.state, intent(), 100_000.0)
        self.assertEqual(len(trade.legs), 3)
        self.assertEqual(trade.targets, list(intent().targets))

    def test_be_33_33_34_refuses_rather_than_quietly_becoming_one_leg(self):
        """Asking for an exit the balance cannot place is an error worth saying
        out loud — silently downgrading it is the bug this setting prevents."""
        settings = replace(Settings(), exit_mode="be_33_33_34")
        broker = FakeBroker()
        small = BotState(initial_balance=6_000.0)
        small.roll_day(date(2026, 6, 1), 6_000.0, 6_000.0)
        self.assertIsNone(trader.open_trade(broker, settings, small, intent(), 6_000.0))
        self.assertEqual(broker.sent, [])
        events = [__import__("json").loads(line) for line
                  in trader.JOURNAL_PATH.read_text(encoding="utf-8").splitlines()]
        self.assertIn("exit_mode_rejected", [e["event"] for e in events])

    def test_leg_weights_follow_the_mode(self):
        base = Settings()
        self.assertEqual(replace(base, exit_mode="fixed_tp3").leg_weights, (1.0,))
        self.assertEqual(replace(base, exit_mode="be_33_33_34").leg_weights,
                         (0.33, 0.33, 0.34))

    def test_a_single_leg_trade_never_moves_its_stop_to_entry(self):
        """One leg has no TP1 to bank; a break-even move would exit at entry a
        trade that fixed_tp3 holds to TP3."""
        settings = replace(Settings(), exit_mode="fixed_tp3")
        broker = FakeBroker()
        trader.open_trade(broker, settings, self.state, intent(), 100_000.0)
        broker._positions = []          # whatever happens to it
        trader.apply_breakeven(broker, self.state)
        self.assertEqual(broker.stops_moved, [])


class ExpirationStampTests(unittest.TestCase):
    """The field that silently killed every limit order on FTMO."""

    def test_expiration_is_seconds_not_a_datetime(self):
        """Was: `expiration` carried a datetime and MetaTrader5 answered
        `(-2) Invalid "expiration" argument`, so no retracement entry ever
        reached the broker — while the bot's own log read as if it had."""
        from bot.code.broker import Broker

        stamp = Broker._expiration_stamp(datetime(2026, 7, 28, 8, 35, 31))
        self.assertIsInstance(stamp, int)
        self.assertNotIsInstance(stamp, bool)

    def test_the_stamp_uses_the_same_clock_tick_time_does(self):
        """`tick()` reads the server's wall clock as though it were UTC, so the
        inverse must too — otherwise a UTC+3 server expires orders three hours
        early or late."""
        from datetime import timezone

        from bot.code.broker import Broker

        moment = datetime(2026, 7, 28, 8, 35, 31)
        stamp = Broker._expiration_stamp(moment)
        # Decode it the way Broker.tick() decodes quote.time.
        back = datetime.fromtimestamp(stamp, tz=timezone.utc).replace(tzinfo=None)
        self.assertEqual(back, moment)

    def test_four_hours_of_server_time_is_four_hours_of_stamp(self):
        from bot.code.broker import Broker

        a = Broker._expiration_stamp(datetime(2026, 7, 28, 4, 35, 31))
        b = Broker._expiration_stamp(datetime(2026, 7, 28, 8, 35, 31))
        self.assertEqual(b - a, 4 * 3600)


class StaleFeedTests(unittest.TestCase):
    """A frozen tick must not be read as the broker changing timezone."""

    def _broker(self, stamps):
        """A Broker whose tick() walks through `stamps`, nothing else wired up."""
        from bot.code.broker import Broker

        b = Broker.__new__(Broker)
        b._requests = 0
        b._tick_stamp = None
        b._tick_first_seen = None
        seq = iter(stamps)
        b.tick = lambda: {"server_time": next(seq)}
        return b

    def test_a_moving_tick_is_not_stale(self):
        base = datetime(2026, 7, 27, 23, 45)
        b = self._broker([base, base + timedelta(minutes=15)])
        self.assertIsNone(b.feed_stale_minutes())
        self.assertIsNone(b.feed_stale_minutes())

    def test_a_frozen_tick_is_reported_once_past_the_threshold(self):
        frozen = datetime(2026, 7, 27, 23, 49, 57)
        b = self._broker([frozen] * 3)
        self.assertIsNone(b.feed_stale_minutes())      # first sighting
        # Pretend the local clock moved on without the tick following.
        b._tick_first_seen -= timedelta(minutes=40)
        self.assertIsNone(b.feed_stale_minutes(threshold_minutes=60))
        self.assertAlmostEqual(b.feed_stale_minutes(threshold_minutes=10), 40, delta=1)

    def test_a_half_hour_lag_no_longer_reads_as_a_timezone_change(self):
        """Was: a tick stale by exactly 30 minutes made a UTC+3 server measure
        2.5, which rounds cleanly and passed the residue check — so the offset
        was rewritten mid-session and every news window moved with it."""
        from datetime import timezone

        from bot.code.broker import Broker

        true_offset, lag = 3, timedelta(minutes=30)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        frozen = now + timedelta(hours=true_offset) - lag

        b = Broker.__new__(Broker)
        b._requests = 0
        b._tick_stamp = frozen
        b._tick_first_seen = now - timedelta(minutes=40)
        b.tick = lambda: {"server_time": frozen}

        # The old arithmetic would have produced a clean 2.5 here.
        raw = (frozen - now).total_seconds() / 3600
        self.assertAlmostEqual(round(raw * 2) / 2, 2.5, places=3)
        # The stale check must veto it instead.
        self.assertIsNone(b.server_utc_offset())


class MarketHoursTests(unittest.TestCase):
    """The no-open window before a closure, holidays included."""

    def setUp(self):
        self.settings = Settings()

    def test_the_weekly_close_is_found_from_any_starting_day(self):
        # 2026-07-27 is a Monday; the close is the Friday of the same week.
        closure = market_hours.next_closure(self.settings, datetime(2026, 7, 27, 9, 0))
        self.assertEqual(closure.start, datetime(2026, 7, 31, 23, 0))
        self.assertEqual(closure.label, "weekly close")
        self.assertGreater(closure.hours, 2.0)

    def test_entries_stop_three_hours_before_the_weekly_close(self):
        allowed = datetime(2026, 7, 31, 19, 30)     # Friday, 3.5h to go
        blocked = datetime(2026, 7, 31, 20, 30)     # Friday, 2.5h to go
        self.assertTrue(guardrails.entry_window_open(self.settings, allowed))
        verdict = guardrails.entry_window_open(self.settings, blocked)
        self.assertFalse(verdict)
        self.assertIn("gap trading", verdict.reason)

    def test_the_weekend_reads_as_closed_rather_than_as_a_blackout(self):
        verdict = guardrails.entry_window_open(self.settings, datetime(2026, 8, 1, 10, 0))
        self.assertFalse(verdict)
        self.assertIn("market is closed", verdict.reason)
    def test_a_configured_holiday_blocks_the_three_hours_before_it(self):
        holiday = replace_closures(self.settings, ([
            "2026-12-24 20:00", "2026-12-28 01:00", "Christmas"
        ],))
        self.assertTrue(guardrails.entry_window_open(
            holiday, datetime(2026, 12, 24, 16, 0)))
        verdict = guardrails.entry_window_open(
            holiday, datetime(2026, 12, 24, 18, 0))
        self.assertFalse(verdict)
        self.assertIn("Christmas", verdict.reason)

    def test_a_whole_day_closure_can_be_written_as_a_bare_date(self):
        holiday = replace_closures(self.settings, ("2026-12-25",))
        shut = market_hours.is_closed(holiday, datetime(2026, 12, 25, 12, 0))
        self.assertIsNotNone(shut)
        self.assertIn("2026-12-25", shut.label)

    def test_a_short_break_is_not_gap_trading(self):
        brief = replace_closures(self.settings, ([
            "2026-07-29 20:00", "2026-07-29 21:00", "rollover"
        ],))
        self.assertTrue(guardrails.entry_window_open(
            brief, datetime(2026, 7, 29, 19, 0)))

    def test_bad_closure_rows_are_dropped_not_fatal(self):
        broken = replace_closures(self.settings, ("not-a-date", [], ["2026-13-45"]))
        self.assertEqual(market_hours.configured_closures(broken), [])
        self.assertTrue(guardrails.entry_window_open(
            broken, datetime(2026, 7, 29, 12, 0)))

    def test_the_real_week_end_is_measured_from_bars(self):
        stamps = pd.date_range("2026-05-04", "2026-07-24 23:45", freq="15min")
        frame = pd.DataFrame({"time": stamps[(stamps.weekday < 5) & (stamps.hour < 21)]})
        day, moment = market_hours.observed_week_end(frame)
        self.assertEqual(day, 4)
        self.assertEqual((moment.hour, moment.minute), (21, 0))

    def test_a_week_ending_at_midnight_is_reported_on_the_right_day(self):
        stamps = pd.date_range("2026-05-04", "2026-07-24 23:45", freq="15min")
        frame = pd.DataFrame({"time": stamps[stamps.weekday < 5]})
        day, moment = market_hours.observed_week_end(frame)
        self.assertEqual(day, 5)
        self.assertEqual((moment.hour, moment.minute), (0, 0))

    def test_an_unmeasurable_frame_returns_none_rather_than_guessing(self):
        self.assertIsNone(market_hours.observed_week_end(None))
        self.assertIsNone(market_hours.observed_week_end(
            pd.DataFrame({"time": [pd.Timestamp("2026-05-04")]})))

def replace_closures(settings, closures):
    from dataclasses import replace

    return replace(settings, market_closures=closures)
