"""Tests for the order layer — the part that was previously untested.

`bot/code/trader.py` is where a plan becomes real orders and where every managed exit
lives, so it is also where a mistake costs money. Nothing here touches
MetaTrader: `FakeBroker` records what would have been sent and can be told to
reject a specific leg, which is the failure that used to strand half a trade.

Each test names the defect it pins down, because these all started as real bugs.
"""
from __future__ import annotations

import io
import json
import copy
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.code import guardrails, market_hours, run, signals, trader  # noqa: E402
from bot.code.broker import Broker, OrderRejected, SymbolSpec  # noqa: E402
from bot.code.instance_lock import LiveInstanceLock  # noqa: E402
from bot.code.settings import Settings  # noqa: E402
from bot.code.signals import Intent  # noqa: E402
from bot.code.state import (BotState, ManagedTrade, ftmo_day,
                            ftmo_day_start_server)  # noqa: E402
from xau.mt5_source import MT5Error  # noqa: E402

GOLD = SymbolSpec(name="XAUUSDm", digits=3, point=0.001, volume_min=0.01,
                  volume_max=50.0, volume_step=0.01, value_per_point=100.0,
                  stops_level_points=0.0, filling=0)


class LiveInstanceLockTests(unittest.TestCase):
    def test_only_one_process_lock_can_be_held(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".live.lock"
            first = LiveInstanceLock(path)
            second = LiveInstanceLock(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_every_live_entry_point_uses_the_same_instance_lock(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(run, "LIVE_LOCK_PATH",
                                  Path(directory) / ".live.lock"):
            held = LiveInstanceLock(run.LIVE_LOCK_PATH)
            self.assertTrue(held.acquire())
            try:
                with self.assertRaisesRegex(
                        SystemExit, "another Quantum Desk LIVE process"):
                    run.execute(live=True)
            finally:
                held.release()

    def test_live_lock_is_released_when_startup_fails(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(run, "LIVE_LOCK_PATH",
                                  Path(directory) / ".live.lock"), \
                mock.patch.object(run.settings_module, "load",
                                  side_effect=MT5Error("settings unavailable")):
            with self.assertRaisesRegex(MT5Error, "settings unavailable"):
                run.execute(live=True)

            probe = LiveInstanceLock(run.LIVE_LOCK_PATH)
            self.assertTrue(probe.acquire())
            probe.release()


@dataclass
class FakePos:
    ticket: int
    direction: int = 1
    volume: float = 0.01
    price_open: float = 4000.0
    stop: float = 3984.0
    take_profit: float = 4016.0
    profit: float = 0.0
    swap: float = 0.0
    symbol: str = "XAUUSDm"
    comment: str = ""
    opened_at: datetime = datetime(2026, 7, 27, 12, 0)


@dataclass
class FakeBroker:
    """Records writes instead of sending them."""
    dry_run: bool = False
    spec: SymbolSpec = GOLD
    symbol_key: str = "XAUUSD"
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
            raise OrderRejected(f"{what} rejected: retcode=10016 Invalid stops")

    def market_entry(self, direction, volume, stop, take_profit, comment,
                     worst_price=None) -> dict:
        price = self.tick()["ask"] if direction == 1 else self.tick()["bid"]
        if worst_price is not None and (
                (direction == 1 and price > worst_price)
                or (direction == -1 and price < worst_price)):
            raise OrderRejected("market price passed the per-leg limit")
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
        # whatever the default happens to be. These paths require the split
        # policy explicitly so a future default change cannot make them vacuous.
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

    def test_each_pending_ticket_is_durable_before_the_next_leg_is_sent(self):
        broker = FakeBroker()
        snapshots = []

        def capture(_path):
            snapshots.append(copy.deepcopy(self.state))

        with mock.patch.object(self.state, "save", side_effect=capture):
            trade = trader.open_trade(
                broker, self.settings, self.state, intent("limit"), 100_000.0,
            )

        saved_counts = [
            len(snapshot.trades[trade.plan_id].pending_tickets)
            for snapshot in snapshots
        ]
        self.assertIn(1, saved_counts)
        self.assertIn(2, saved_counts)
        self.assertIn(3, saved_counts)

    def test_each_market_order_id_is_durable_before_the_next_leg_is_sent(self):
        broker = FakeBroker()
        snapshots = []

        def capture(_path):
            snapshots.append(copy.deepcopy(self.state))

        with mock.patch.object(self.state, "save", side_effect=capture):
            trade = trader.open_trade(
                broker, self.settings, self.state, intent(), 100_000.0,
            )

        saved_counts = [
            len(snapshot.trades[trade.plan_id].market_order_tickets)
            for snapshot in snapshots
        ]
        self.assertIn(1, saved_counts)
        self.assertIn(2, saved_counts)
        self.assertIn(3, saved_counts)

    def test_delayed_market_visibility_stays_open_until_history_recovers_it(self):
        class DelayedVisibilityBroker(FakeBroker):
            def market_entry(self, direction, volume, stop, take_profit, comment,
                             worst_price=None):
                self._guard(f"market {volume}")
                ticket = self._ticket()
                # MT5 accepted the order, but positions_get() has not exposed
                # its resulting position yet.
                return {"dry_run": False, "order": ticket, "deal": ticket}

        broker = DelayedVisibilityBroker()
        output = io.StringIO()
        with redirect_stdout(output):
            trade = trader.open_trade(
                broker, self.settings, self.state, intent(), 100_000.0,
            )

        orders = list(trade.market_order_tickets)
        self.assertEqual(len(orders), 3)
        self.assertFalse(trade.closed)
        self.assertIn("orders accepted; waiting for MT5 position visibility",
                      output.getvalue())
        self.assertEqual(
            run.active_setup_count(self.state, broker.positions(),
                                   broker.pending_orders()),
            1,
        )
        self.assertTrue(run.needs_split_management(self.state))

        # Deal history can lag too. The first reconciliation must retain every
        # id and retry, not close the plan as an empty trade.
        trader.sync_fills(broker, self.state)
        self.assertEqual(trade.market_order_tickets, orders)
        self.assertFalse(trade.closed)

        broker.filled_orders = dict(zip(orders, (301, 302, 303)))
        broker._positions = [
            FakePos(ticket=302, stop=trade.stop, take_profit=trade.targets[1]),
            FakePos(ticket=303, stop=trade.stop, take_profit=trade.targets[2]),
        ]
        trader.sync_fills(broker, self.state)
        trader.apply_breakeven(broker, self.state)

        self.assertEqual(trade.market_order_tickets, [])
        self.assertEqual(trade.tp1_position_ticket, 301)
        self.assertEqual(sorted(broker.stops_moved),
                         [(302, 4000.12), (303, 4000.12)])
        self.assertTrue(trade.breakeven_done)

    def test_deal_only_market_result_stays_managed_until_position_is_visible(self):
        """Some market executions identify the fill by deal, not by order."""
        class DealOnlyDelayedBroker(FakeBroker):
            def market_entry(self, direction, volume, stop, take_profit, comment,
                             worst_price=None):
                self._guard(f"market {volume}")
                deal_ticket = self._ticket()
                position_ticket = deal_ticket + 1_000
                # Deal history knows the mapping, but positions_get() is still
                # lagging when open_trade performs its immediate readback.
                self.filled_orders[deal_ticket] = position_ticket
                return {"dry_run": False, "order": 0, "deal": deal_ticket}

        broker = DealOnlyDelayedBroker()
        with redirect_stdout(io.StringIO()):
            trade = trader.open_trade(
                broker, self.settings, self.state, intent(), 100_000.0,
            )

        recovery_tickets = list(trade.market_order_tickets)
        self.assertEqual(len(recovery_tickets), 3)
        self.assertFalse(trade.closed)

        # Exercise the restart representation, not only the in-memory object.
        restarted = BotState.load(Path(trader.STATE_PATH))
        recovered = restarted.trades[trade.plan_id]
        self.assertFalse(recovered.closed)
        trader.sync_fills(broker, restarted)
        self.assertEqual(recovered.market_order_tickets, [])
        self.assertEqual(
            recovered.position_tickets,
            [broker.filled_orders[ticket] for ticket in recovery_tickets],
        )
        self.assertEqual(
            recovered.tp1_position_ticket,
            broker.filled_orders[recovery_tickets[0]],
        )

    def test_delayed_converted_fill_actualizes_risk_from_entry_deals(self):
        """Visibility timing must not change the R denominator for one fill."""
        class DelayedConvertedBroker(FakeBroker):
            def market_entry(self, direction, volume, stop, take_profit, comment,
                             worst_price=None):
                self._guard(f"market {volume}")
                deal_ticket = self._ticket()
                position_ticket = deal_ticket + 1_000
                executed_volume = volume / 2
                self.filled_orders[deal_ticket] = position_ticket
                self.deals.append({
                    "ticket": deal_ticket, "order": deal_ticket,
                    "position": position_ticket, "volume": executed_volume,
                    "price": 4000.20, "is_exit": False,
                    "profit": 0.0, "commission": 0.0, "swap": 0.0,
                    "fee": 0.0, "net": 0.0,
                })
                return {"dry_run": False, "order": 0, "deal": deal_ticket,
                        "volume": executed_volume}

        broker = DelayedConvertedBroker()
        converted = replace(intent(), converted=True)
        with redirect_stdout(io.StringIO()):
            trade = trader.open_trade(
                broker, self.settings, self.state, converted, 100_000.0,
            )
        conservative = trade.risk_cash
        self.assertTrue(trade.converted_risk_pending)
        complete_deals = list(broker.deals)
        broker.deals = complete_deals[:2]

        restarted = BotState.load(Path(trader.STATE_PATH))
        recovered = restarted.trades[trade.plan_id]
        with redirect_stdout(io.StringIO()):
            trader.sync_fills(broker, restarted)
        self.assertTrue(recovered.converted_risk_pending)
        self.assertEqual(recovered.risk_cash, conservative)

        broker.deals = complete_deals
        with redirect_stdout(io.StringIO()):
            trader.sync_fills(broker, restarted)
        expected = (sum(deal["volume"] for deal in complete_deals)
                    * (converted.risk + 0.20) * GOLD.value_per_point)
        self.assertAlmostEqual(recovered.risk_cash, expected, places=2)
        self.assertLess(recovered.risk_cash, conservative)
        self.assertFalse(recovered.converted_risk_pending)

        events = [json.loads(line) for line in
                  Path(trader.JOURNAL_PATH).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events[-1]["event"], "risk_cash_actualized")

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

    def test_out_of_order_pending_fills_keep_the_real_tp1_identity(self):
        broker = FakeBroker()
        trade = trader.open_trade(
            broker, self.settings, self.state, intent("limit"), 100_000.0,
        )
        tp1_order, tp2_order, _ = list(trade.pending_tickets)

        tp2_position = broker.fill_order(tp2_order)
        trader.sync_fills(broker, self.state, {"M15": bars(300)})
        tp1_position = broker.fill_order(tp1_order)
        trader.sync_fills(broker, self.state, {"M15": bars(300)})

        # Position discovery order is TP2 then TP1, but the durable identity
        # must still point at the actual TP1 leg.
        self.assertEqual(trade.position_tickets[:2], [tp2_position, tp1_position])
        self.assertEqual(trade.tp1_position_ticket, tp1_position)

        broker._positions = [
            position for position in broker._positions
            if position.ticket != tp1_position
        ]
        trader.apply_breakeven(broker, self.state)

        self.assertIn((tp2_position, 4000.12), broker.stops_moved)
        self.assertTrue(trade.breakeven_done)

    def test_unresolved_real_tp1_identity_never_uses_tp2_as_a_fallback(self):
        trade = ManagedTrade(
            plan_id="M15@tp1-unresolved", timeframe="M15", direction=1,
            entry=4000.0, stop=3984.0, risk=16.0, risk_cash=400.0,
            targets=[4016.0, 4024.0, 4032.0], legs=[0.08, 0.08, 0.09],
            position_tickets=[302, 303],
            market_order_tickets=[201],
            tp1_market_order_ticket=201,
            exit_mode="be_33_33_34",
        )
        self.state.trades[trade.plan_id] = trade
        # TP2 has disappeared, TP3 survives. TP1 is still unresolved, so TP2's
        # absence cannot be used as evidence that TP1 was banked.
        broker = FakeBroker(_positions=[
            FakePos(ticket=303, stop=trade.stop, take_profit=trade.targets[2]),
        ])

        trader.apply_breakeven(broker, self.state)

        self.assertEqual(broker.stops_moved, [])
        self.assertFalse(trade.breakeven_done)

    def test_dry_run_fast_wait_does_not_score_visible_real_positions_as_closed(self):
        broker = FakeBroker()
        trade = trader.open_trade(
            broker, self.settings, self.state, intent(), 100_000.0,
        )
        first, *_ = trade.position_tickets
        broker._positions = [
            position for position in broker._positions
            if position.ticket != first
        ]
        broker.dry_run = True
        broker.requests = 0
        broker.take_requests = lambda: 0
        config_ = SimpleNamespace(
            split_management_poll_seconds=1,
            max_requests_per_day=2_000,
        )

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(
                    run, "STATE_PATH", Path(folder) / "state.json",
                ), \
                mock.patch.object(run.time, "sleep"), \
                mock.patch.object(trader, "reconcile_closed") as reconcile:
            run.sleep_and_manage_split(broker, self.state, config_, 2)

        reconcile.assert_not_called()
        self.assertFalse(trade.closed)

    def test_market_order_history_recovers_tickets_after_a_hard_placement_crash(self):
        trade = ManagedTrade(
            plan_id="M15@crash", timeframe="M15", direction=1,
            entry=4000.0, stop=3984.0, risk=16.0, risk_cash=400.0,
            targets=[4016.0, 4024.0, 4032.0], legs=[0.08, 0.08, 0.09],
            market_order_tickets=[201, 202, 203],
            tp1_market_order_ticket=201, exit_mode="be_33_33_34",
        )
        self.state.trades[trade.plan_id] = trade
        broker = FakeBroker(
            _positions=[
                FakePos(ticket=302, stop=3984.0, take_profit=4024.0),
                FakePos(ticket=303, stop=3984.0, take_profit=4032.0),
            ],
            filled_orders={201: 301, 202: 302, 203: 303},
        )

        trader.sync_fills(broker, self.state, {"M15": bars(300)})
        self.assertEqual(trade.position_tickets, [301, 302, 303])
        self.assertEqual(trade.tp1_position_ticket, 301)
        self.assertEqual(trade.market_order_tickets, [])

        trader.apply_breakeven(broker, self.state)
        self.assertEqual(
            sorted(broker.stops_moved),
            [(302, 4000.12), (303, 4000.12)],
        )
        self.assertTrue(trade.breakeven_done)
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
        self.assertTrue(all(stop == 4000.12 for _, stop in broker.stops_moved))
        self.assertTrue(trade.breakeven_done)

    def test_breakeven_uses_each_market_leg_actual_fill_not_signal_entry(self):
        broker = FakeBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        first, second, third = trade.position_tickets
        broker._positions = [
            position for position in broker._positions if position.ticket != first
        ]
        broker._positions[0].price_open = 4002.03
        broker._positions[1].price_open = 4002.08

        trader.apply_breakeven(broker, self.state)

        self.assertEqual(
            broker.stops_moved,
            [(second, 4002.15), (third, 4002.20)],
        )
        self.assertTrue(trade.breakeven_done)

    def test_negative_swap_tightens_an_already_completed_breakeven(self):
        broker = FakeBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        first, second, third = trade.position_tickets
        broker._positions = [
            position for position in broker._positions if position.ticket != first
        ]
        trader.apply_breakeven(broker, self.state)
        self.assertEqual(
            broker.stops_moved,
            [(second, 4000.12), (third, 4000.12)],
        )

        # MT5 reports swap as cumulative cash. Derive it from the actual lots:
        # market entries are deliberately sized for their worst permitted fill.
        broker._positions[0].swap = -0.20 * 100 * broker._positions[0].volume
        broker._positions[1].swap = -0.30 * 100 * broker._positions[1].volume
        trader.apply_breakeven(broker, self.state)

        self.assertEqual(
            broker.stops_moved[-2:],
            [(second, 4000.32), (third, 4000.42)],
        )
        self.assertTrue(trade.breakeven_done)

    def test_negative_swap_tightens_sell_breakeven_away_from_the_losing_side(self):
        broker = FakeBroker(_positions=[
            FakePos(ticket=402, direction=-1, volume=0.08,
                    price_open=4000.0, stop=4016.0, take_profit=3976.0,
                    swap=-1.60),
            FakePos(ticket=403, direction=-1, volume=0.09,
                    price_open=4000.0, stop=4016.0, take_profit=3968.0,
                    swap=-2.70),
        ])
        trade = ManagedTrade(
            plan_id="M15@sell-swap", timeframe="M15", direction=-1,
            entry=4000.0, stop=4016.0, risk=16.0, risk_cash=400.0,
            targets=[3984.0, 3976.0, 3968.0], legs=[0.08, 0.08, 0.09],
            position_tickets=[401, 402, 403],
            tp1_position_ticket=401,
            breakeven_done=True, exit_mode="be_33_33_34",
        )
        self.state.trades[trade.plan_id] = trade

        trader.apply_breakeven(broker, self.state)

        self.assertEqual(
            broker.stops_moved,
            [(402, 3999.68), (403, 3999.58)],
        )
        self.assertTrue(trade.breakeven_done)

    def test_positive_swap_never_loosens_a_cost_covered_stop(self):
        broker = FakeBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        first, *_ = trade.position_tickets
        broker._positions = [
            position for position in broker._positions if position.ticket != first
        ]
        trader.apply_breakeven(broker, self.state)
        original_moves = list(broker.stops_moved)

        for position in broker._positions:
            position.swap = 10.0
        trader.apply_breakeven(broker, self.state)

        self.assertEqual(broker.stops_moved, original_moves)

    def test_one_rejected_breakeven_move_does_not_abort_the_other_survivor(self):
        """The first refusal escaped the loop, so the other survivor kept its
        original stop even though the broker would have accepted its move. The
        trade must remain retryable until every survivor is protected."""
        class PickyBroker(FakeBroker):
            def move_stop(self, position, stop):
                if position.ticket == rejected:
                    raise OrderRejected("move stop rejected: retcode=10004 Requote")
                return super().move_stop(position, stop)

        broker = PickyBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        first, rejected, accepted = trade.position_tickets
        broker._positions = [position for position in broker._positions if position.ticket != first]
        trader.apply_breakeven(broker, self.state)
        self.assertEqual(broker.stops_moved, [(accepted, 4000.12)])
        self.assertFalse(trade.breakeven_done)

    def test_breakeven_transport_failure_propagates_for_reconnect(self):
        class DisconnectedBroker(FakeBroker):
            def move_stop(self, position, stop):
                raise MT5Error(
                    "move stop got no result: (-10005) IPC timeout"
                )

        broker = DisconnectedBroker()
        trade = trader.open_trade(
            broker, self.settings, self.state, intent(), 100_000.0,
        )
        first, *_ = trade.position_tickets
        broker._positions = [
            position for position in broker._positions
            if position.ticket != first
        ]

        with self.assertRaisesRegex(MT5Error, "IPC timeout"):
            trader.apply_breakeven(broker, self.state)

        self.assertFalse(trade.breakeven_done)

    def test_partial_breakeven_is_retried_until_every_survivor_is_protected(self):
        class RetryBroker(FakeBroker):
            refused_once = False

            def move_stop(self, position, stop):
                if position.ticket == retried and not self.refused_once:
                    self.refused_once = True
                    raise OrderRejected("move stop rejected: retcode=10004 Requote")
                return super().move_stop(position, stop)

        broker = RetryBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        first, retried, accepted = trade.position_tickets
        broker._positions = [position for position in broker._positions if position.ticket != first]

        trader.apply_breakeven(broker, self.state)
        self.assertFalse(trade.breakeven_done)
        self.assertEqual(broker.stops_moved, [(accepted, 4000.12)])

        trader.apply_breakeven(broker, self.state)
        self.assertTrue(trade.breakeven_done)
        self.assertEqual(
            broker.stops_moved,
            [(accepted, 4000.12), (retried, 4000.12)],
        )

    def test_a_stale_done_flag_is_repaired_when_a_survivor_is_not_at_be(self):
        broker = FakeBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        first, *survivors = trade.position_tickets
        broker._positions = [
            position for position in broker._positions if position.ticket != first
        ]
        # A prior release could write this after only one successful move.
        trade.breakeven_done = True

        trader.apply_breakeven(broker, self.state)

        self.assertEqual(
            sorted(ticket for ticket, _ in broker.stops_moved),
            sorted(survivors),
        )
        self.assertTrue(trade.breakeven_done)

    def test_a_failed_stale_flag_repair_reactivates_fast_polling(self):
        class RefusingBroker(FakeBroker):
            def move_stop(self, position, stop):
                raise OrderRejected("move stop rejected: retcode=10018 Market closed")

        broker = RefusingBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        first, *_ = trade.position_tickets
        broker._positions = [
            position for position in broker._positions if position.ticket != first
        ]
        trade.breakeven_done = True

        trader.apply_breakeven(broker, self.state)

        self.assertFalse(trade.breakeven_done)
        self.assertTrue(run.needs_split_management(self.state))

    def test_fast_split_wait_reconciles_tickets_before_checking_be(self):
        trade = ManagedTrade(
            plan_id="M15@waiting", timeframe="M15", direction=1,
            entry=4000.0, stop=3984.0, risk=16.0, risk_cash=400.0,
            targets=[4016.0, 4024.0, 4032.0], legs=[0.08, 0.08, 0.09],
            pending_tickets=[201, 202, 203], exit_mode="be_33_33_34",
        )
        self.state.trades[trade.plan_id] = trade
        broker = FakeBroker()
        broker.requests = 0
        broker.take_requests = lambda: 0
        config_ = SimpleNamespace(
            split_management_poll_seconds=1,
            max_requests_per_day=2_000,
        )

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(run, "STATE_PATH",
                                  Path(folder) / "state.json"), \
                mock.patch.object(run.time, "sleep"), \
                mock.patch.object(trader, "sync_fills") as sync, \
                mock.patch.object(trader, "apply_breakeven") as apply:
            run.sleep_and_manage_split(broker, self.state, config_, 2)

        sync.assert_called_once_with(broker, self.state)
        apply.assert_called_once_with(broker, self.state)

    def test_fast_split_wait_refreshes_swap_without_ticket_history_requests(self):
        trade = ManagedTrade(
            plan_id="M15@swap-refresh", timeframe="M15", direction=1,
            entry=4000.0, stop=3984.0, risk=16.0, risk_cash=400.0,
            targets=[4016.0, 4024.0, 4032.0], legs=[0.08, 0.08, 0.09],
            position_tickets=[101, 102, 103], tp1_position_ticket=101,
            breakeven_done=True, exit_mode="be_33_33_34",
        )
        self.state.trades[trade.plan_id] = trade
        broker = FakeBroker()
        broker.requests = 0
        broker.take_requests = lambda: 0
        config_ = SimpleNamespace(
            split_management_poll_seconds=1,
            max_requests_per_day=2_000,
        )

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(run, "STATE_PATH",
                                  Path(folder) / "state.json"), \
                mock.patch.object(run.time, "sleep"), \
                mock.patch.object(trader, "sync_fills") as sync, \
                mock.patch.object(
                    trader, "apply_breakeven",
                    return_value={101, 102, 103}) as apply, \
                mock.patch.object(trader, "reconcile_closed") as reconcile:
            run.sleep_and_manage_split(broker, self.state, config_, 2)

        sync.assert_not_called()
        apply.assert_called_once_with(broker, self.state)
        reconcile.assert_not_called()

    def test_fast_split_wait_scores_survivors_closed_between_signal_bars(self):
        trade = ManagedTrade(
            plan_id="M15@fast-close", timeframe="M15", direction=1,
            entry=4000.0, stop=3984.0, risk=16.0, risk_cash=400.0,
            targets=[4016.0, 4024.0, 4032.0], legs=[0.08, 0.08, 0.09],
            position_tickets=[101, 102, 103], tp1_position_ticket=101,
            filled_at="2026-07-27 12:00:00",
            breakeven_done=True, exit_mode="be_33_33_34",
        )
        self.state.trades[trade.plan_id] = trade
        broker = FakeBroker(_positions=[])
        broker.deals = [
            {
                "position": ticket, "profit": 10.0, "commission": -1.0,
                "swap": -0.5, "net": 8.5,
            }
            for ticket in trade.position_tickets
        ]
        broker.requests = 0
        broker.take_requests = lambda: 0
        config_ = SimpleNamespace(
            split_management_poll_seconds=1,
            max_requests_per_day=2_000,
        )

        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(run, "STATE_PATH",
                                  Path(folder) / "state.json"), \
                mock.patch.object(run.time, "sleep"):
            run.sleep_and_manage_split(broker, self.state, config_, 2)

        self.assertTrue(trade.closed)
        self.assertAlmostEqual(self.state.day_realised, 25.5)
        self.assertFalse(run.needs_split_management(self.state))

    def test_dry_run_cannot_mark_real_survivors_as_protected(self):
        broker = FakeBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        first, *_ = trade.position_tickets
        broker._positions = [
            position for position in broker._positions if position.ticket != first
        ]
        broker.dry_run = True

        live_tickets = trader.apply_breakeven(broker, self.state)

        self.assertEqual(broker.stops_moved, [])
        self.assertFalse(trade.breakeven_done)
        self.assertEqual(live_tickets, {
            position.ticket for position in broker._positions
        })

    def test_be_comparison_uses_the_cost_covered_price_sent_to_the_broker(self):
        broker = FakeBroker(_positions=[
            FakePos(ticket=102, stop=4000.12),
            FakePos(ticket=103, stop=4000.12),
        ])
        trade = ManagedTrade(
            plan_id="M15@rounded", timeframe="M15", direction=1,
            entry=4000.0004, stop=3984.0, risk=16.0004, risk_cash=400.0,
            targets=[4016.0, 4024.0, 4032.0], legs=[0.08, 0.08, 0.09],
            position_tickets=[101, 102, 103], exit_mode="be_33_33_34",
        )
        self.state.trades[trade.plan_id] = trade

        trader.apply_breakeven(broker, self.state)

        self.assertEqual(broker.stops_moved, [])
        self.assertTrue(trade.breakeven_done)

    def test_sell_survivor_with_no_stop_is_not_mistaken_for_protected(self):
        broker = FakeBroker(_positions=[
            FakePos(ticket=402, direction=-1, stop=0.0, take_profit=3976.0),
            FakePos(ticket=403, direction=-1, stop=0.0, take_profit=3968.0),
        ])
        trade = ManagedTrade(
            plan_id="M15@sell-no-stop", timeframe="M15", direction=-1,
            entry=4000.0, stop=4016.0, risk=16.0, risk_cash=400.0,
            targets=[3984.0, 3976.0, 3968.0], legs=[0.08, 0.08, 0.09],
            position_tickets=[401, 402, 403],
            tp1_position_ticket=401,
            exit_mode="be_33_33_34",
        )
        self.state.trades[trade.plan_id] = trade

        trader.apply_breakeven(broker, self.state)

        self.assertEqual(sorted(broker.stops_moved),
                         [(402, 3999.88), (403, 3999.88)])
        self.assertTrue(trade.breakeven_done)

    def test_startup_reconciliation_moves_survivors_after_offline_tp1(self):
        class StartupBroker(FakeBroker):
            requests = 0

            def account(self):
                return {
                    "login": 1, "server": "test",
                    "balance": 100_000.0, "equity": 100_000.0,
                }

            def bars(self, timeframe, count):
                return bars(count)

            def take_requests(self):
                return 0

        broker = StartupBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        first, *survivors = trade.position_tickets
        broker._positions = [
            position for position in broker._positions if position.ticket != first
        ]
        config_ = SimpleNamespace(
            timeframes=("M15",), history_bars=300, initial_balance=100_000.0,
        )

        with mock.patch.object(
                run, "_bind_and_anchor",
                return_value=(broker.tick(), broker.account())), \
                mock.patch.object(run, "STATE_PATH",
                                  Path(self.tmp.name) / "startup-state.json"), \
                mock.patch.object(run, "JOURNAL_PATH",
                                  Path(self.tmp.name) / "startup-journal.jsonl"):
            run.reconcile_startup(broker, self.state, config_)

        self.assertEqual(
            sorted(ticket for ticket, _ in broker.stops_moved),
            sorted(survivors),
        )
        self.assertTrue(trade.breakeven_done)

    def test_startup_scores_positions_that_closed_while_bot_was_offline(self):
        class StartupBroker(FakeBroker):
            requests = 0

            def account(self):
                return {
                    "login": 1, "server": "test",
                    "balance": 100_050.0, "equity": 100_050.0,
                }

            def bars(self, timeframe, count):
                return bars(count)

            def take_requests(self):
                return 0

        broker = StartupBroker()
        trade = trader.open_trade(
            broker, self.settings, self.state, intent(), 100_000.0,
        )
        broker._positions = []
        broker.deals = [
            {
                "position": ticket, "profit": 20.0, "commission": -1.0,
                "swap": -0.5, "net": 18.5,
            }
            for ticket in trade.position_tickets
        ]
        config_ = SimpleNamespace(
            timeframes=("M15",), history_bars=300, initial_balance=100_000.0,
        )

        with mock.patch.object(
                run, "_bind_and_anchor",
                return_value=(broker.tick(), broker.account())), \
                mock.patch.object(run, "STATE_PATH",
                                  Path(self.tmp.name) / "startup-state.json"), \
                mock.patch.object(run, "JOURNAL_PATH",
                                  Path(self.tmp.name) / "startup-journal.jsonl"):
            run.reconcile_startup(broker, self.state, config_)

        self.assertTrue(trade.closed)
        self.assertAlmostEqual(self.state.day_realised, 55.5)
        self.assertEqual(run.active_setup_count(
            self.state, broker.positions(), broker.pending_orders()), 0)

    def test_startup_protects_known_survivors_even_if_bar_loading_fails(self):
        class FailingBarsBroker(FakeBroker):
            requests = 0

            def account(self):
                return {
                    "login": 1, "server": "test",
                    "balance": 100_000.0, "equity": 100_000.0,
                }

            def bars(self, timeframe, count):
                raise MT5Error("price history unavailable")

        broker = FailingBarsBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        tp1 = trade.tp1_position_ticket
        survivors = [
            ticket for ticket in trade.position_tickets if ticket != tp1
        ]
        broker._positions = [
            position for position in broker._positions if position.ticket != tp1
        ]
        config_ = SimpleNamespace(
            timeframes=("M15",), history_bars=300, initial_balance=100_000.0,
        )

        with mock.patch.object(
                run, "_bind_and_anchor",
                return_value=(broker.tick(), broker.account())):
            with self.assertRaisesRegex(MT5Error, "price history unavailable"):
                run.reconcile_startup(broker, self.state, config_)

        self.assertEqual(
            sorted(ticket for ticket, _ in broker.stops_moved),
            sorted(survivors),
        )

    def test_startup_recovers_crash_tickets_before_failed_bar_loading(self):
        class FailingBarsBroker(FakeBroker):
            requests = 0

            def account(self):
                return {
                    "login": 1, "server": "test",
                    "balance": 100_000.0, "equity": 100_000.0,
                }

            def bars(self, timeframe, count):
                raise MT5Error("price history unavailable")

            def take_requests(self):
                return 0

        trade = ManagedTrade(
            plan_id="M15@startup-crash", timeframe="M15", direction=1,
            entry=4000.0, stop=3984.0, risk=16.0, risk_cash=400.0,
            targets=[4016.0, 4024.0, 4032.0], legs=[0.08, 0.08, 0.09],
            market_order_tickets=[201, 202, 203],
            tp1_market_order_ticket=201, exit_mode="be_33_33_34",
        )
        self.state.trades[trade.plan_id] = trade
        broker = FailingBarsBroker(
            _positions=[
                FakePos(ticket=302, stop=3984.0, take_profit=4024.0),
                FakePos(ticket=303, stop=3984.0, take_profit=4032.0),
            ],
            filled_orders={201: 301, 202: 302, 203: 303},
        )
        config_ = SimpleNamespace(
            timeframes=("M15",), history_bars=300, initial_balance=100_000.0,
        )

        with mock.patch.object(
                run, "_bind_and_anchor",
                return_value=(broker.tick(), broker.account())):
            with self.assertRaisesRegex(MT5Error, "price history unavailable"):
                run.reconcile_startup(broker, self.state, config_)

        self.assertEqual(trade.tp1_position_ticket, 301)
        self.assertEqual(
            sorted(broker.stops_moved),
            [(302, 4000.12), (303, 4000.12)],
        )

    def test_all_rejected_breakeven_moves_leave_the_trade_retryable(self):
        """The unconditional flag said break-even was done after a refusal, so
        the next pass never tried to protect the remaining legs again."""
        class RefusingBroker(FakeBroker):
            def move_stop(self, position, stop):
                raise OrderRejected("move stop rejected: retcode=10018 Market closed")

        broker = RefusingBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        first, *_ = trade.position_tickets
        broker._positions = [position for position in broker._positions if position.ticket != first]
        trader.apply_breakeven(broker, self.state)
        self.assertFalse(trade.breakeven_done)

    def test_breakeven_does_nothing_when_the_stop_took_every_leg(self):
        broker = FakeBroker()
        trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        broker._positions = []          # SL hit: all three gone at once
        trader.apply_breakeven(broker, self.state)
        self.assertEqual(broker.stops_moved, [])

    def test_one_rejected_timeout_close_does_not_abort_the_other_trade(self):
        """A refused timeout close stopped the management pass before later
        trades received the same 120-bar exit attempt."""
        class PickyBroker(FakeBroker):
            def close(self, position, reason):
                if position.ticket == rejected:
                    raise OrderRejected("close rejected: retcode=10018 Market closed")
                return super().close(position, reason)

        broker = PickyBroker()
        first_trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        second_trade = trader.open_trade(
            broker, self.settings, self.state,
            replace(intent(), plan_id="M15@2026-06-01 00:15:00"), 100_000.0)
        for trade in (first_trade, second_trade):
            trade.fill_bar_time = str(bars(1)["time"].iloc[0])
        rejected = first_trade.position_tickets[0]
        trader.enforce_timeout(broker, self.state, {"M15": bars(350)})
        self.assertIn(second_trade.position_tickets[0], [ticket for ticket, _ in broker.closed])

    def test_a_rejected_stale_cancel_keeps_the_ticket_managed(self):
        """The unconditional clear forgot a still-working order after its cancel
        was refused, so it could fill without a managed trade owning it."""
        class RefusingBroker(FakeBroker):
            def cancel(self, ticket, reason):
                raise OrderRejected("cancel rejected: retcode=10018 Market closed")

        broker = RefusingBroker()
        trade = trader.open_trade(broker, self.settings, self.state,
                                  intent("limit"), 100_000.0)
        ticket = trade.pending_tickets[0]
        trader.cancel_stale(broker, self.state,
                            replace(intent("limit"), status="BUY STALE"))
        self.assertIn(ticket, trade.pending_tickets)
        self.assertFalse(trade.closed)

    def test_one_rejected_flatten_close_does_not_abort_the_others(self):
        """The first emergency-close refusal escaped, leaving later positions
        open while the operator line incorrectly claimed the account was flat."""
        class PickyBroker(FakeBroker):
            def close(self, position, reason):
                if position.ticket == 701:
                    raise OrderRejected("close rejected: retcode=10018 Market closed")
                return super().close(position, reason)

        broker = PickyBroker(_positions=[FakePos(ticket=ticket) for ticket in (701, 702, 703)])
        output = io.StringIO()
        with redirect_stdout(output):
            trader.flatten_all(broker, self.state, "manual flatten")
        self.assertEqual([ticket for ticket, _ in broker.closed], [702, 703])
        self.assertIn("incomplete; refused=[701]", output.getvalue())
        self.assertIn('"event": "flatten_rejected"',
                      (Path(self.tmp.name) / "journal.jsonl").read_text(encoding="utf-8"))

    def test_missing_deal_history_does_not_close_trade_at_zero(self):
        broker = FakeBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        broker._positions = []
        broker.deals = []
        finished = trader.reconcile_closed(broker, self.state)
        self.assertEqual(finished, [])
        self.assertFalse(trade.closed)
        self.assertEqual(self.state.day_realised, 0.0)

    def test_an_opening_deal_does_not_masquerade_as_a_closed_position(self):
        """Deal history can lead positions_get while a fresh fill is publishing."""
        broker = FakeBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        broker._positions = []
        broker.deals = [
            {
                "position": ticket, "profit": 0.0, "commission": -1.0,
                "swap": 0.0, "net": -1.0, "is_exit": False,
            }
            for ticket in trade.position_tickets
        ]

        finished = trader.reconcile_closed(broker, self.state)

        self.assertEqual(finished, [])
        self.assertFalse(trade.closed)
        self.assertEqual(self.state.day_realised, 0.0)

    def test_partial_market_history_cannot_close_the_whole_setup(self):
        trade = ManagedTrade(
            plan_id="M15@partial-history", timeframe="M15", direction=1,
            entry=4000.0, stop=3984.0, risk=16.0, risk_cash=400.0,
            targets=[4016.0, 4024.0, 4032.0], legs=[0.08, 0.08, 0.09],
            position_tickets=[301], market_order_tickets=[202, 203],
            tp1_position_ticket=301, exit_mode="be_33_33_34",
            filled_at="2026-07-27 12:00:00",
        )
        self.state.trades[trade.plan_id] = trade
        broker = FakeBroker(deals=[
            {
                "position": 301, "profit": 100.0, "commission": -1.0,
                "swap": 0.0, "net": 99.0,
            },
        ])

        finished = trader.reconcile_closed(broker, self.state)

        self.assertEqual(finished, [])
        self.assertFalse(trade.closed)
        self.assertEqual(self.state.day_realised, 0.0)

    def test_partial_exit_history_cannot_score_a_fully_mapped_setup(self):
        broker = FakeBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        broker._positions = []
        broker.deals = [
            {
                "position": ticket, "volume": volume,
                "profit": 10.0, "commission": -1.0,
                "swap": 0.0, "net": 9.0, "is_exit": True,
            }
            for ticket, volume in zip(trade.position_tickets[:2], trade.legs[:2])
        ]

        finished = trader.reconcile_closed(broker, self.state)

        self.assertEqual(finished, [])
        self.assertFalse(trade.closed)
        self.assertEqual(self.state.day_realised, 0.0)

    def test_partial_exit_volume_cannot_score_a_position_early(self):
        broker = FakeBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        broker._positions = []
        opening_deals = [
            {
                "position": ticket, "volume": volume,
                "profit": 0.0, "commission": -1.0,
                "swap": 0.0, "net": -1.0, "is_exit": False,
            }
            for ticket, volume in zip(trade.position_tickets, trade.legs)
        ]
        exit_deals = [
            {
                "position": ticket,
                "volume": volume / 2 if index == 0 else volume,
                "profit": 10.0, "commission": -1.0,
                "swap": 0.0, "net": 9.0, "is_exit": True,
            }
            for index, (ticket, volume) in enumerate(
                zip(trade.position_tickets, trade.legs)
            )
        ]
        broker.deals = opening_deals + exit_deals

        finished = trader.reconcile_closed(broker, self.state)

        self.assertEqual(finished, [])
        self.assertFalse(trade.closed)
        self.assertEqual(self.state.day_realised, 0.0)

    def test_complete_exit_scores_opening_and_closing_costs(self):
        broker = FakeBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        broker._positions = []
        broker.deals = []
        for ticket, volume in zip(trade.position_tickets, trade.legs):
            broker.deals.extend((
                {
                    "position": ticket, "volume": volume,
                    "profit": 0.0, "commission": -1.0,
                    "swap": 0.0, "net": -1.0, "is_exit": False,
                },
                {
                    "position": ticket, "volume": volume,
                    "profit": 10.0, "commission": -1.0,
                    "swap": 0.0, "net": 9.0, "is_exit": True,
                },
            ))

        finished = trader.reconcile_closed(broker, self.state)

        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["profit"], 24.0)
        self.assertEqual(finished[0]["costs"], -6.0)

    def test_r_is_scored_after_commission_swap_and_fee(self):
        """Was: only `deal.profit` counted, so every live R read better than the
        money in the account — the exact number used to judge the edge."""
        broker = FakeBroker()
        trade = trader.open_trade(broker, self.settings, self.state, intent(), 100_000.0)
        tickets = list(trade.position_tickets)
        broker._positions = []
        broker.deals = [
            {"position": tickets[0], "profit": 400.0, "commission": -7.0,
             "swap": -3.0, "fee": -1.0, "net": 389.0},
            {"position": tickets[1], "profit": 200.0, "commission": -7.0,
             "swap": -3.0, "fee": -1.0, "net": 189.0},
            {"position": tickets[2], "profit": -200.0, "commission": -7.0,
             "swap": -3.0, "fee": -1.0, "net": -211.0},
        ]
        finished = trader.reconcile_closed(broker, self.state)
        self.assertEqual(len(finished), 1)
        record = finished[0]
        self.assertAlmostEqual(record["profit"], 367.0)
        self.assertAlmostEqual(record["costs"], -33.0)
        self.assertAlmostEqual(record["r"], 367.0 / trade.risk_cash, places=4)
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


    def test_position_health_names_plan_target_and_exit_distances(self):
        state = BotState(initial_balance=10_000.0)
        trade = ManagedTrade(
            plan_id="M15@health", timeframe="M15", direction=-1,
            entry=4033.0, stop=4047.9359, risk=14.9359, risk_cash=42.0,
            targets=[4005.0], legs=[0.03], position_tickets=[701],
            exit_mode="fixed_tp3",
        )
        state.trades[trade.plan_id] = trade
        position = FakePos(
            ticket=701, direction=-1, volume=0.03, price_open=4033.0,
            stop=4047.94, take_profit=4005.0)
        health = run.position_health(
            position, state,
            {"bid": 4029.8, "ask": 4030.0,
             "server_time": datetime(2026, 7, 28)})
        self.assertIn("plan=M15@health", health)
        self.assertIn("mode=fixed_tp3", health)
        self.assertIn("role=TP3", health)
        self.assertIn("status=PROTECTED", health)
        self.assertIn("now=+0.20R", health)

    def test_position_health_flags_missing_protection(self):
        state = BotState(initial_balance=10_000.0)
        trade = ManagedTrade(
            plan_id="M15@naked", timeframe="M15", direction=1,
            entry=4000.0, stop=3984.0, risk=16.0, risk_cash=40.0,
            targets=[4032.0], legs=[0.02], position_tickets=[702],
            exit_mode="fixed_tp3",
        )
        state.trades[trade.plan_id] = trade
        naked = FakePos(ticket=702, stop=0.0, take_profit=0.0)
        health = run.position_health(
            naked, state,
            {"bid": 4001.0, "ask": 4001.2,
             "server_time": datetime(2026, 7, 28)})
        self.assertIn("MISSING_SL", health)
        self.assertIn("MISSING_TP", health)

    def test_floating_pnl_includes_negative_swap(self):
        positions = [
            FakePos(ticket=710, profit=12.50, swap=-1.25),
            FakePos(ticket=711, profit=-2.00, swap=-0.50),
        ]
        self.assertEqual(run.floating_pnl(positions), 8.75)

    def test_position_health_rejects_signal_entry_as_fill_breakeven(self):
        state = BotState(initial_balance=50_000.0)
        trade = ManagedTrade(
            plan_id="M15@bad-be", timeframe="M15", direction=1,
            entry=4088.77, stop=4062.34, risk=26.43, risk_cash=200.0,
            targets=[4115.20, 4128.41, 4141.63], legs=[0.03, 0.02, 0.03],
            position_tickets=[801, 802, 803], tp1_position_ticket=801,
            breakeven_done=True, exit_mode="be_33_33_34",
        )
        state.trades[trade.plan_id] = trade

        bad = run.position_health(
            FakePos(ticket=802, price_open=4091.30, stop=4088.77),
            state,
            {"bid": 4103.0, "ask": 4103.4,
             "server_time": datetime(2026, 7, 30)},
        )
        repaired = run.position_health(
            FakePos(ticket=803, price_open=4091.27, stop=4091.39),
            state,
            {"bid": 4103.0, "ask": 4103.4,
             "server_time": datetime(2026, 7, 30)},
        )
        stale_after_swap = run.position_health(
            FakePos(ticket=802, volume=0.02, price_open=4091.30,
                    stop=4091.42, swap=-0.40),
            state,
            {"bid": 4103.0, "ask": 4103.4,
             "server_time": datetime(2026, 7, 30)},
            FakeBroker(),
        )

        self.assertIn("BE_STOP_BELOW_FILL", bad)
        self.assertIn("SL_AT_NET_BE", repaired)
        self.assertIn("BE_STOP_BELOW_NET", stale_after_swap)

    def test_entry_capacity_reports_risk_room_and_allowed_side(self):
        settings = replace(Settings(), exit_mode="fixed_tp3")
        state = BotState(initial_balance=10_000.0)
        trade = ManagedTrade(
            plan_id="M15@capacity", timeframe="M15", direction=-1,
            entry=4033.0, stop=4047.0, risk=14.0, risk_cash=42.0,
            targets=[4005.0], legs=[0.03], position_tickets=[703],
            exit_mode="fixed_tp3",
        )
        state.trades[trade.plan_id] = trade
        position = FakePos(
            ticket=703, direction=-1, volume=0.03, price_open=4033.0,
            stop=4047.0, take_profit=4005.0)
        allowed, message = run.entry_capacity(
            settings, state, [position], [], GOLD, 10_000.0, requests=100)
        self.assertFalse(allowed)
        self.assertIn("risk room", message)

        position.volume = 0.02
        allowed, message = run.entry_capacity(
            settings, state, [position], [], GOLD, 10_000.0, requests=100)
        self.assertTrue(allowed)
        self.assertIn("SELL only", message)


class ReconnectTests(unittest.TestCase):
    def test_reconnect_shuts_down_old_session_and_rebuilds_connection(self):
        from bot.code.broker import Broker

        class OldTerminal:
            stopped = False

            def shutdown(self):
                self.stopped = True

        broker = Broker("XAUUSD", magic=1, deviation=30, dry_run=True)
        old = OldTerminal()
        broker._mt = old
        rebuilt = []
        broker._connect = lambda: rebuilt.append(True)

        broker.reconnect()

        self.assertTrue(old.stopped)
        self.assertIsNone(broker._mt)
        self.assertIsNone(broker._spec)
        self.assertEqual(rebuilt, [True])

    def test_an_order_rejection_does_not_reconnect(self):
        """A rejected write was treated as a lost connection, so a permanent
        broker refusal entered a pointless reconnect and backoff loop."""
        class LoopBroker:
            dry_run = True
            requests = 0
            spec = GOLD

            def __init__(self):
                self.reconnects = 0

            def account(self):
                return {"login": 1, "server": "test"}

            def tick(self):
                return {"server_time": datetime(2026, 7, 27, 12, 0)}

            def positions(self):
                return []

            def pending_orders(self):
                return []

            def feed_stale_minutes(self):
                return None

            def reconnect(self):
                self.reconnects += 1

            def take_requests(self):
                return 0

        broker = LoopBroker()
        config_ = SimpleNamespace(symbol="XAUUSDm", timeframes=("M15",),
                                  entry_grace_seconds=0, initial_balance=0.0)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(run, "JOURNAL_PATH", Path(directory) / "journal.jsonl"), \
                mock.patch.object(run, "STATE_PATH", Path(directory) / "state.json"), \
                mock.patch.object(run, "sleep_and_manage_split"), \
                mock.patch.object(run, "pass_once",
                                  side_effect=[OrderRejected("close rejected"),
                                               KeyboardInterrupt()]):
            with redirect_stdout(io.StringIO()):
                run.loop(broker, BotState(), config_)
        self.assertEqual(broker.reconnects, 0)

    def test_connection_loss_checkpoints_requests_before_reconnect(self):
        class LoopBroker:
            dry_run = True
            requests = 7
            spec = GOLD

            def __init__(self):
                self.reconnects = 0
                self.tick_calls = 0
                self.uncheckpointed = 7

            def account(self):
                return {"login": 1, "server": "test"}

            def tick(self):
                self.tick_calls += 1
                if self.tick_calls == 1:
                    return {"server_time": datetime(2026, 7, 27, 12, 0)}
                raise KeyboardInterrupt()

            def positions(self):
                raise MT5Error("positions_get failed: (-10005) IPC timeout")

            def pending_orders(self):
                return []

            def reconnect(self):
                self.reconnects += 1

            def take_requests(self):
                spent, self.uncheckpointed = self.uncheckpointed, 0
                self.requests = 0
                return spent

        broker = LoopBroker()
        state = BotState()
        config_ = SimpleNamespace(
            symbol="XAUUSDm", timeframes=("M15",),
            entry_grace_seconds=0, initial_balance=0.0,
            reconnect_initial_seconds=1, reconnect_max_seconds=2,
        )
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with mock.patch.object(run, "JOURNAL_PATH",
                                   Path(directory) / "journal.jsonl"), \
                    mock.patch.object(run, "STATE_PATH", state_path), \
                    mock.patch.object(run.time, "sleep"), \
                    mock.patch.object(run, "pass_once"):
                with redirect_stdout(io.StringIO()):
                    run.loop(broker, state, config_)

            saved = BotState.load(state_path)

        self.assertEqual(broker.reconnects, 1)
        self.assertEqual(state.day_requests, 7)
        self.assertEqual(saved.day_requests, 7)


class OrphanTimeoutTests(unittest.TestCase):
    """A position whose local record was lost used to be held forever."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for attr in ("STATE_PATH", "JOURNAL_PATH"):
            patcher = mock.patch.object(trader, attr, Path(self.tmp.name) / attr.lower())
            patcher.start()
            self.addCleanup(patcher.stop)
        self.frames = {"M15": bars(300, 15), "M30": bars(300, 30)}
        self.old = self.frames["M15"]["time"].iloc[0].to_pydatetime()
        self.recent = self.frames["M15"]["time"].iloc[-3].to_pydatetime()

    def _managed(self, ticket: int) -> BotState:
        state = BotState(initial_balance=50_000.0)
        state.trades["p1"] = ManagedTrade(
            plan_id="p1", timeframe="M15", direction=1, entry=4000.0, stop=3984.0,
            risk=16.0, risk_cash=200.0, targets=[4016.0], legs=[0.01],
            exit_mode="fixed_tp3", position_tickets=[ticket],
            fill_bar_time=str(self.frames["M15"]["time"].iloc[-1]))
        return state

    def test_an_unmanaged_position_is_closed_once_its_own_clock_runs_out(self):
        broker = FakeBroker(_positions=[
            FakePos(ticket=999, comment="M15 TP2 quantum|be33", opened_at=self.old)])
        trader.enforce_orphan_timeout(broker, BotState(initial_balance=50_000.0),
                                      self.frames)
        self.assertEqual([ticket for ticket, _ in broker.closed], [999])

    def test_an_unmanaged_position_inside_its_window_is_left_alone(self):
        broker = FakeBroker(_positions=[
            FakePos(ticket=999, comment="M15 TP2 quantum|be33", opened_at=self.recent)])
        trader.enforce_orphan_timeout(broker, BotState(initial_balance=50_000.0),
                                      self.frames)
        self.assertEqual(broker.closed, [])

    def test_a_complete_three_leg_bot_setup_is_recovered_into_state(self):
        broker = FakeBroker(_positions=[
            FakePos(ticket=501, direction=-1, volume=0.03, price_open=4037.87,
                    stop=4057.35, take_profit=4017.74,
                    comment="M15 TP1 quantum|be33", opened_at=self.recent),
            FakePos(ticket=502, direction=-1, volume=0.03, price_open=4037.87,
                    stop=4057.35, take_profit=4007.83,
                    comment="M15 TP2 quantum|be33", opened_at=self.recent),
            FakePos(ticket=503, direction=-1, volume=0.04, price_open=4037.87,
                    stop=4057.35, take_profit=3997.93,
                    comment="M15 TP3 quantum|be33", opened_at=self.recent),
        ])
        state = BotState(initial_balance=50_000.0)
        state.roll_day(date(2026, 7, 31), 50_000.0, 50_000.0)

        recovered = trader.recover_orphan_setups(broker, state)

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].position_tickets, [501, 502, 503])
        self.assertEqual(recovered[0].tp1_position_ticket, 501)
        self.assertAlmostEqual(recovered[0].risk_cash, 194.80)

    def test_an_incomplete_or_ambiguous_setup_is_not_recovered(self):
        broker = FakeBroker(_positions=[
            FakePos(ticket=501, comment="M15 TP1 quantum|be33"),
            FakePos(ticket=503, comment="M15 TP3 quantum|be33"),
        ])
        self.assertEqual(
            trader.recover_orphan_setups(
                broker, BotState(initial_balance=50_000.0),
            ),
            [],
        )

    def test_a_managed_position_is_left_to_enforce_timeout(self):
        """Both paths firing would send two closes for one position, and the
        second would be a close on a ticket that no longer exists."""
        broker = FakeBroker(_positions=[
            FakePos(ticket=111, comment="M15 TP2 quantum|be33", opened_at=self.old)])
        trader.enforce_orphan_timeout(broker, self._managed(111), self.frames)
        self.assertEqual(broker.closed, [])

    def test_a_record_marked_closed_no_longer_shields_a_live_position(self):
        """`open_trades()` skips closed records, so a position that outlived its
        record fell through both paths and was held indefinitely."""
        state = self._managed(111)
        state.trades["p1"].closed = True
        broker = FakeBroker(_positions=[
            FakePos(ticket=111, comment="M15 TP2 quantum|be33", opened_at=self.old)])
        trader.enforce_orphan_timeout(broker, state, self.frames)
        self.assertEqual([ticket for ticket, _ in broker.closed], [111])

    def test_an_unreadable_comment_falls_back_to_the_slowest_timeframe(self):
        """Guessing M15 would close a position 120 M30 bars early. The slower
        frame is the safe guess: it can only delay the close."""
        self.assertEqual(trader._orphan_timeframe("", self.frames), "M30")
        self.assertEqual(trader._orphan_timeframe("hand edited", self.frames), "M30")
        self.assertEqual(trader._orphan_timeframe("M15 TP1 quantum", self.frames), "M15")

    def test_bars_are_counted_not_hours_so_a_weekend_does_not_expire_a_trade(self):
        """120 M30 bars is 60 *trading* hours. Counting wall clock instead would
        time out anything held over a weekend the moment Monday opened."""
        frame = bars(200, 30)
        gap = frame.copy()
        gap.loc[100:, "time"] = gap.loc[100:, "time"] + pd.Timedelta(days=2)
        opened = frame["time"].iloc[50].to_pydatetime()
        self.assertEqual(signals.bars_since_moment(gap, opened),
                         signals.bars_since_moment(frame, opened))

    def test_a_new_trade_does_not_adopt_a_position_that_was_already_live(self):
        """`known` was built from `state.trades`, which assumed every live
        position had a record. An orphan has none, so it was swept into the next
        trade: scored against that trade's risk_cash, its stop moved to that
        trade's entry, and hidden from the orphan timeout that would close it."""
        orphan = FakePos(ticket=777, direction=-1, volume=0.05,
                         comment="M15 TP1 quantum|be33", opened_at=self.old)
        broker = FakeBroker(_positions=[orphan])
        trade = trader.open_trade(broker, Settings(exit_mode="fixed_tp3"),
                                  BotState(initial_balance=50_000.0),
                                  intent(), 50_000.0)
        self.assertNotIn(777, trade.position_tickets)
        self.assertEqual(len(trade.position_tickets), 1)

    def test_one_unclosable_orphan_does_not_abort_the_others(self):
        """A refused close used to escape the pass and reach `run.loop`, which
        reads every MT5Error as a lost connection and answers with a reconnect —
        no help against a rejection, and the second orphan never got its turn."""
        class PickyBroker(FakeBroker):
            def close(self, position, reason):
                if position.ticket == 901:
                    raise OrderRejected("close rejected: retcode=10018 Market closed")
                return super().close(position, reason)

        broker = PickyBroker(_positions=[
            FakePos(ticket=ticket, comment="M30 TP1 quantum|be33", opened_at=self.old)
            for ticket in (901, 902)])
        trader.enforce_orphan_timeout(broker, BotState(initial_balance=50_000.0),
                                      self.frames)
        self.assertEqual([ticket for ticket, _ in broker.closed], [902])

    def test_positions_from_another_source_are_never_touched(self):
        """`broker.positions()` filters on this bot's magic, so an EA's or a
        hand-opened position is invisible here and stays that way."""
        broker = FakeBroker(_positions=[])
        trader.enforce_orphan_timeout(broker, BotState(initial_balance=50_000.0),
                                      self.frames)
        self.assertEqual(broker.closed, [])


class BrokerReadFailureTests(unittest.TestCase):
    """An MT5 read error must never masquerade as an empty live book."""

    @staticmethod
    def terminal(result):
        return SimpleNamespace(
            POSITION_TYPE_BUY=0,
            ORDER_TYPE_BUY_LIMIT=2,
            ORDER_TYPE_SELL_LIMIT=3,
            ORDER_TYPE_BUY_STOP=4,
            ORDER_TYPE_SELL_STOP=5,
            ORDER_STATE_FILLED=4,
            ORDER_STATE_CANCELED=2,
            ORDER_STATE_REJECTED=3,
            ORDER_STATE_EXPIRED=6,
            positions_get=mock.Mock(return_value=result),
            orders_get=mock.Mock(return_value=result),
            history_deals_get=mock.Mock(return_value=result),
            history_orders_get=mock.Mock(return_value=result),
            last_error=mock.Mock(return_value=(-10005, "IPC timeout")),
        )

    def broker(self, result):
        broker = Broker("XAUUSD", magic=1, deviation=30, dry_run=False)
        broker._mt = self.terminal(result)
        broker._spec = GOLD
        return broker

    def test_none_from_mt5_reads_raises_instead_of_reporting_no_exposure(self):
        cases = (
            ("positions", lambda broker: broker.positions()),
            ("orders", lambda broker: broker.pending_orders()),
            ("order mapping",
             lambda broker: broker.filled_order_positions([101])),
            ("order states",
             lambda broker: broker.finished_order_states([101])),
            ("closed deals",
             lambda broker: broker.closed_deals(datetime(2026, 7, 30))),
            ("account cash flow",
             lambda broker: broker.account_cashflow_since(datetime(2026, 7, 30))),
        )
        for name, operation in cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(MT5Error, "IPC timeout"):
                    operation(self.broker(None))

    def test_empty_mt5_sequences_still_mean_no_exposure_or_history(self):
        broker = self.broker(())

        self.assertEqual(broker.positions(), [])
        self.assertEqual(broker.pending_orders(), [])
        self.assertEqual(broker.filled_order_positions([101]), {})
        self.assertEqual(broker.finished_order_states([101]), {})
        self.assertEqual(
            broker.closed_deals(datetime(2026, 7, 30)),
            [],
        )
        self.assertEqual(
            broker.account_cashflow_since(datetime(2026, 7, 30)),
            0.0,
        )

    def test_account_cashflow_reconstructs_midnight_balance_across_all_magic(self):
        before = SimpleNamespace(
            time=int(datetime(2026, 7, 31, 0, 59, tzinfo=timezone.utc).timestamp()),
            profit=100.0, commission=-1.0, swap=0.0, fee=0.0,
        )
        after_bot = SimpleNamespace(
            time=int(datetime(2026, 7, 31, 1, 1, tzinfo=timezone.utc).timestamp()),
            profit=50.0, commission=-2.0, swap=-0.5, fee=-0.25,
        )
        after_other = SimpleNamespace(
            time=int(datetime(2026, 7, 31, 2, 0, tzinfo=timezone.utc).timestamp()),
            profit=-20.0, commission=-1.0, swap=0.0, fee=0.0,
        )
        broker = self.broker((before, after_bot, after_other))

        result = broker.account_cashflow_since(datetime(2026, 7, 31, 1, 0))

        self.assertEqual(result, 26.25)

    def test_account_reports_whether_mt5_uses_hedging_positions(self):
        broker = self.broker(())
        broker._mt.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
        account = SimpleNamespace(
            login=1, server="test", currency="USD", balance=50_000.0,
            equity=50_000.0, margin_free=49_000.0, margin_mode=2,
        )
        broker._mt.account_info = mock.Mock(return_value=account)
        self.assertTrue(broker.account()["is_hedging"])

        account.margin_mode = 0
        result = broker.account()
        self.assertFalse(result["is_hedging"])
        self.assertEqual(result["margin_mode"], 0)

    def test_deal_ticket_can_recover_its_position_without_an_order_ticket(self):
        deal = SimpleNamespace(
            ticket=701, order=0, position_id=901, magic=1, symbol=GOLD.name,
        )
        broker = self.broker((deal,))

        self.assertEqual(broker.filled_order_positions([701]), {701: 901})

    def test_closed_deal_net_includes_the_mt5_fee_field(self):
        deal = SimpleNamespace(
            ticket=1, order=2, position_id=3, volume=0.01, price=4000.0,
            profit=10.0, commission=-1.0, swap=-0.50, fee=-0.25,
            comment="test", entry=1, time=1_775_000_000,
            magic=1, symbol=GOLD.name,
        )
        broker = self.broker((deal,))

        result = broker.closed_deals(datetime(2026, 7, 30))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["fee"], -0.25)
        self.assertEqual(result[0]["net"], 8.25)
        self.assertTrue(result[0]["is_exit"])

    def test_opening_deal_is_classified_separately_from_an_exit(self):
        deal = SimpleNamespace(
            ticket=1, order=2, position_id=3, volume=0.01, price=4000.0,
            profit=0.0, commission=-1.0, swap=0.0, fee=0.0,
            comment="test", entry=0, time=1_775_000_000,
            magic=1, symbol=GOLD.name,
        )
        broker = self.broker((deal,))

        result = broker.closed_deals(datetime(2026, 7, 30))

        self.assertEqual(len(result), 1)
        self.assertFalse(result[0]["is_exit"])


class WriteSpacingTests(unittest.TestCase):
    """Three legs leaving in the same instant reads as order flooding."""

    def _broker(self, **kwargs):
        from bot.code.broker import Broker

        return Broker("XAUUSD", magic=1, deviation=30, **kwargs)

    def test_a_second_write_waits_out_the_remaining_gap(self):
        broker = self._broker(dry_run=False, write_spacing_seconds=2.0)
        slept = []
        with mock.patch("bot.code.broker.time.sleep", slept.append), \
                mock.patch("bot.code.broker.time.monotonic", return_value=100.5):
            broker._last_write = None
            broker._pace()                      # first write never waits
            self.assertEqual(slept, [])
            broker._last_write = 100.0
            broker._pace()                      # 0.5s elapsed of the 2.0s gap
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 1.5)

    def test_a_gap_already_served_does_not_sleep(self):
        broker = self._broker(dry_run=False, write_spacing_seconds=2.0)
        slept = []
        broker._last_write = 100.0
        with mock.patch("bot.code.broker.time.sleep", slept.append), \
                mock.patch("bot.code.broker.time.monotonic", return_value=105.0):
            broker._pace()
        self.assertEqual(slept, [])

    def test_a_dry_run_never_pays_the_delay(self):
        broker = self._broker(dry_run=True, write_spacing_seconds=2.0)
        slept = []
        broker._last_write = 100.0
        with mock.patch("bot.code.broker.time.sleep", slept.append), \
                mock.patch("bot.code.broker.time.monotonic", return_value=100.1):
            broker._pace()
        self.assertEqual(slept, [])

    def test_the_wait_happens_before_the_quote_is_read(self):
        """Sleeping after `tick()` would price the order off a stale quote:
        gold moves in two seconds, and the deviation guard would reject or the
        fill would land somewhere the plan never asked for."""
        broker = self._broker(dry_run=False, write_spacing_seconds=2.0)
        order = []
        broker._pace = lambda: order.append("pace")
        broker.tick = lambda: (order.append("tick"),
                               {"ask": 3300.0, "bid": 3299.5})[1]
        broker._send = lambda request, what: order.append("send") or {}
        broker._spec = GOLD
        broker._mt = SimpleNamespace(TRADE_ACTION_DEAL=1, ORDER_TYPE_BUY=0,
                                     ORDER_TYPE_SELL=1, ORDER_TIME_GTC=0)

        broker.market_entry(1, 0.01, 3290.0, 3320.0, "M15 TP3")
        broker.close(FakePos(ticket=1), "timeout")

        self.assertEqual(order, ["pace", "tick", "send"] * 2)

    def test_market_entry_rejects_a_quote_past_the_per_leg_price_limit(self):
        broker = self._broker(dry_run=False, write_spacing_seconds=2.0)
        broker._spec = GOLD
        broker._mt = SimpleNamespace(TRADE_ACTION_DEAL=1, ORDER_TYPE_BUY=0,
                                     ORDER_TYPE_SELL=1, ORDER_TIME_GTC=0)
        broker._pace = lambda: None
        broker._send = mock.Mock(return_value={})

        cases = (
            (1, {"ask": 3300.01, "bid": 3299.50}, 3300.00),
            (-1, {"ask": 3300.50, "bid": 3299.99}, 3300.00),
        )
        for direction, quote, worst_price in cases:
            with self.subTest(direction=direction):
                broker.tick = lambda quote=quote: quote
                with self.assertRaisesRegex(OrderRejected, "per-leg limit"):
                    broker.market_entry(direction, 0.01, 3290.0, 3320.0,
                                        "M15 TP3", worst_price=worst_price)

        broker._send.assert_not_called()

    def test_broker_allows_a_sell_be_stop_when_the_position_has_no_sl(self):
        broker = self._broker(dry_run=False)
        broker._spec = GOLD
        broker._mt = SimpleNamespace(TRADE_ACTION_SLTP=6)
        sent = []
        broker._pace = lambda: None
        broker._send = lambda request, what: sent.append(request) or {}
        position = FakePos(
            ticket=77, direction=-1, stop=0.0, take_profit=3968.0,
        )

        broker.move_stop(position, 4000.0)

        self.assertEqual(sent[0]["position"], 77)
        self.assertEqual(sent[0]["sl"], 4000.0)

    def test_the_stamp_is_taken_when_the_request_leaves_not_when_it_returns(self):
        """A slow `order_send` must not stretch the gap: the broker rates when
        requests arrive, so pacing from the reply would double the real spacing."""
        broker = self._broker(dry_run=False, write_spacing_seconds=2.0)

        class SlowTerminal:
            TRADE_RETCODE_DONE = 10009
            TRADE_RETCODE_PLACED = 10008

            def order_send(self, request):
                return SimpleNamespace(retcode=10009, order=7, deal=0, price=3300.0,
                                       comment="ok")

        broker._mt = SlowTerminal()
        broker._spec = GOLD
        with mock.patch("bot.code.broker.time.monotonic", return_value=100.0):
            broker._send({}, "market buy")
        self.assertEqual(broker._last_write, 100.0)

    def test_a_rejected_write_still_counts_against_the_gap(self):
        """A rejection is a request the broker saw. Retrying it instantly is
        exactly the burst the spacing exists to prevent."""
        broker = self._broker(dry_run=False, write_spacing_seconds=2.0)

        class RejectingTerminal:
            TRADE_RETCODE_DONE = 10009
            TRADE_RETCODE_PLACED = 10008

            def order_send(self, request):
                return SimpleNamespace(retcode=10004, order=0, deal=0, price=0.0,
                                       comment="requote")

        broker._mt = RejectingTerminal()
        broker._spec = GOLD
        with mock.patch("bot.code.broker.time.monotonic", return_value=100.0):
            with self.assertRaises(MT5Error):
                broker._send({}, "market buy")
        self.assertEqual(broker._last_write, 100.0)

    def test_bad_retcode_raises_an_order_rejection_but_no_result_is_an_mt5_error(self):
        """Both failures raised the same error, so the loop reconnected after a
        broker refusal even though only a missing terminal response needed it."""
        self.assertTrue(issubclass(OrderRejected, MT5Error))
        broker = self._broker(dry_run=False)

        class RejectingTerminal:
            TRADE_RETCODE_DONE = 10009
            TRADE_RETCODE_PLACED = 10008

            def order_send(self, request):
                return SimpleNamespace(retcode=10004, order=0, deal=0, price=0.0,
                                       comment="requote")

        broker._mt = RejectingTerminal()
        broker._spec = GOLD
        with self.assertRaises(OrderRejected):
            broker._send({}, "market buy")

        class SilentTerminal(RejectingTerminal):
            def order_send(self, request):
                return None

            def last_error(self):
                return (1, "terminal unavailable")

        broker._mt = SilentTerminal()
        with self.assertRaises(MT5Error) as raised:
            broker._send({}, "market buy")
        self.assertNotIsInstance(raised.exception, OrderRejected)

    def test_spacing_must_not_be_negative(self):
        with self.assertRaises(ValueError):
            Settings(write_spacing_seconds=-1.0)

    def test_split_management_poll_must_be_positive(self):
        with self.assertRaises(ValueError):
            Settings(split_management_poll_seconds=0)


class DailyRulesTests(unittest.TestCase):
    """The day boundary, the loss streak and the daily-loss reference."""

    def setUp(self):
        self.settings = Settings()

    def test_a_loss_streak_clears_on_the_next_ftmo_day(self):
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

    def test_daily_floor_is_midnight_balance_minus_fixed_initial_amount(self):
        state = BotState(initial_balance=100_000.0)
        state.roll_day(date(2026, 6, 1), 102_000.0, 104_000.0)
        self.assertEqual(state.day_start_equity, 104_000.0)
        self.assertEqual(guardrails.daily_loss_floor(self.settings, state), 97_000.0)

    def test_ftmo_day_does_not_roll_at_broker_midnight_in_summer(self):
        self.assertEqual(
            ftmo_day(datetime(2026, 7, 31, 0, 30), 3), date(2026, 7, 30),
        )
        self.assertEqual(
            ftmo_day(datetime(2026, 7, 31, 1, 0), 3), date(2026, 7, 31),
        )
        self.assertEqual(
            ftmo_day_start_server(datetime(2026, 7, 31, 12, 0), 3),
            datetime(2026, 7, 31, 1, 0),
        )

    def test_ftmo_day_does_not_roll_at_broker_midnight_in_winter(self):
        self.assertEqual(
            ftmo_day(datetime(2026, 1, 31, 0, 30), 2), date(2026, 1, 30),
        )
        self.assertEqual(
            ftmo_day_start_server(datetime(2026, 1, 31, 12, 0), 2),
            datetime(2026, 1, 31, 1, 0),
        )

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

    def test_a_netting_account_is_blocked_before_state_is_bound(self):
        class NettingBroker:
            def tick(self):
                return {"server_time": datetime(2026, 7, 31, 12, 0)}

            def account(self):
                return {
                    "login": 99, "server": "test", "currency": "USD",
                    "balance": 50_000.0, "equity": 50_000.0,
                    "margin_free": 50_000.0, "margin_mode": 0,
                    "is_hedging": False,
                }

        state = BotState()
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(run, "JOURNAL_PATH",
                                  Path(folder) / "journal.jsonl"):
            with self.assertRaisesRegex(SystemExit, "requires a hedging account"):
                run._bind_and_anchor(NettingBroker(), state, self.settings)

        self.assertIsNone(state.account_login)


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

    def test_capital_tier_is_fixed_below_30000_and_split_at_30000(self):
        settings = replace(Settings(), exit_mode="capital_tier",
                           split_exit_min_balance=30_000.0)

        below_state = BotState(initial_balance=29_999.99)
        below_state.roll_day(date(2026, 6, 1), 29_999.99, 29_999.99)
        below = trader.open_trade(
            FakeBroker(), settings, below_state, intent(), 29_999.99)
        self.assertEqual(below.exit_mode, "fixed_tp3")
        self.assertEqual(len(below.legs), 1)
        self.assertEqual(below.targets, [intent().targets[2]])

        threshold_state = BotState(initial_balance=30_000.0)
        threshold_state.roll_day(date(2026, 6, 1), 30_000.0, 30_000.0)
        threshold_broker = FakeBroker()
        threshold = trader.open_trade(
            threshold_broker, settings, threshold_state, intent(), 30_000.0)
        self.assertEqual(threshold.exit_mode, "be_33_33_34")
        self.assertEqual(len(threshold.legs), 3)
        self.assertEqual(threshold.targets, list(intent().targets))
        self.assertTrue(run.needs_split_management(threshold_state))

        # TP1 must be confirmed gone before either survivor moves to entry.
        self.assertEqual(threshold_broker.stops_moved, [])
        threshold_broker._positions = threshold_broker._positions[1:]
        trader.apply_breakeven(threshold_broker, threshold_state)
        self.assertEqual(len(threshold_broker.stops_moved), 2)
        self.assertTrue(all(stop == 4000.12
                            for _, stop in threshold_broker.stops_moved))
        self.assertTrue(run.needs_split_management(threshold_state))

    def test_capital_tier_never_flips_when_live_balance_crosses_threshold(self):
        settings = replace(Settings(), exit_mode="capital_tier",
                           split_exit_min_balance=30_000.0)
        state = BotState(initial_balance=29_000.0)
        state.roll_day(date(2026, 6, 1), 29_000.0, 29_000.0)

        # The caller accidentally supplies current balance after profits. The
        # trader must still anchor both sizing and policy to durable state.
        trade = trader.open_trade(FakeBroker(), settings, state, intent(), 31_000.0)
        self.assertEqual(trade.exit_mode, "fixed_tp3")
        self.assertEqual(len(trade.legs), 1)

    def test_split_tier_refuses_a_wide_stop_that_cannot_make_three_legs(self):
        settings = replace(Settings(), exit_mode="capital_tier",
                           split_exit_min_balance=30_000.0)
        state = BotState(initial_balance=30_000.0)
        state.roll_day(date(2026, 6, 1), 30_000.0, 30_000.0)
        wide = replace(intent(), stop=3950.0, risk=50.0,
                       targets=(4050.0, 4075.0, 4100.0))
        broker = FakeBroker()

        self.assertIsNone(trader.open_trade(broker, settings, state, wide, 30_000.0))
        self.assertEqual(broker.sent, [])

    def test_old_state_infers_and_persists_the_concrete_exit_policy(self):
        path = Path(self.tmp.name) / "old-state.json"
        payload = {
            "initial_balance": 10_000.0,
            "trades": {
                "M15@old": {
                    "plan_id": "M15@old", "timeframe": "M15", "direction": -1,
                    "entry": 4033.0, "stop": 4047.0, "risk": 14.0,
                    "risk_cash": 42.0, "targets": [4005.0], "legs": [0.03],
                }
            },
        }
        path.write_text(__import__("json").dumps(payload), encoding="utf-8")

        restored = BotState.load(path)
        self.assertEqual(restored.trades["M15@old"].exit_mode, "fixed_tp3")
        restored.save(path)
        saved = __import__("json").loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["trades"]["M15@old"]["exit_mode"], "fixed_tp3")

    def test_three_tp_tickets_count_as_one_concurrent_setup(self):
        settings = replace(Settings(), exit_mode="capital_tier",
                           split_exit_min_balance=30_000.0)
        broker = FakeBroker()
        trade = trader.open_trade(broker, settings, self.state, intent(), 100_000.0)

        self.assertEqual(len(trade.position_tickets), 3)
        self.assertEqual(run.active_setup_count(
            self.state, broker.positions(), broker.pending_orders()), 1)
        self.assertTrue(guardrails.can_open(
            settings, self.state, open_risk=0.4, open_count=3, pending_count=0,
            active_setups=1))

        orphan = {"ticket": 999, "type": 2, "price": 4000.0, "volume": 0.01}
        self.assertEqual(run.active_setup_count(
            self.state, broker.positions(), [orphan]), 2)
        self.assertFalse(guardrails.can_open(
            settings, self.state, open_risk=0.4, open_count=3, pending_count=1,
            active_setups=2))

    def test_legacy_auto_records_the_policy_it_actually_sent(self):
        settings = replace(Settings(), exit_mode="auto")
        small_state = BotState(initial_balance=6_000.0)
        small_state.roll_day(date(2026, 6, 1), 6_000.0, 6_000.0)
        one = trader.open_trade(
            FakeBroker(), settings, small_state, intent(), 6_000.0)
        self.assertEqual(one.exit_mode, "fixed_tp3")

        large_state = BotState(initial_balance=100_000.0)
        large_state.roll_day(date(2026, 6, 1), 100_000.0, 100_000.0)
        three = trader.open_trade(
            FakeBroker(), settings, large_state, intent(), 100_000.0)
        self.assertEqual(three.exit_mode, "be_33_33_34")

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

class ConvertToMarketTests(unittest.TestCase):
    """Releasing a working limit so the plan can be re-sent at market.

    The expensive failure here is double size: a cancel and a fill can cross on
    the wire, and sending the market order anyway leaves two positions on one
    plan. Every test below is a way that can happen.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self._patched = {}
        for name, value in (("JOURNAL_PATH", base / "journal.jsonl"),
                            ("STATE_PATH", base / "state.json")):
            self._patched[name] = getattr(trader, name)
            setattr(trader, name, value)
        self.state = BotState(initial_balance=50_000.0)
        self.state.roll_day(date(2026, 6, 1), 50_000.0, 50_000.0)

    def tearDown(self):
        for name, value in self._patched.items():
            setattr(trader, name, value)
        self.tmp.cleanup()

    def _working_limit(self, broker: FakeBroker) -> Intent:
        """A plan with one live limit order, as if placed on an earlier bar."""
        result = broker.limit_entry(1, 0.05, 3990.0, 3974.0, 4006.0, None, "M15 TP1")
        plan = intent(action="market")
        plan = replace(plan, converted=True, bars_since_signal=2,
                       fill_bar_time="2026-06-01 00:30:00")
        self.state.trades[plan.plan_id] = ManagedTrade(
            plan_id=plan.plan_id, timeframe="M15", direction=1, entry=3990.0,
            stop=3974.0, risk=16.0, risk_cash=200.0, targets=[4006.0],
            legs=[0.05], dry_run=False, exit_mode="be_33_33_34",
            pending_tickets=[int(result["order"])],
        )
        return plan

    def test_a_confirmed_cancel_clears_the_order_and_allows_the_market_send(self):
        broker = FakeBroker()
        plan = self._working_limit(broker)
        with redirect_stdout(io.StringIO()):
            self.assertTrue(trader.release_for_conversion(broker, self.state, plan))
        self.assertEqual(len(broker.cancelled), 1)
        self.assertEqual(broker.pending_orders(), [])
        self.assertEqual(self.state.trades[plan.plan_id].pending_tickets, [])
        self.assertTrue(self.state.trades[plan.plan_id].closed)
        self.assertTrue(self.state.trades[plan.plan_id].conversion_released)
        events = [json.loads(line) for line in
                  Path(trader.JOURNAL_PATH).read_text(encoding="utf-8").splitlines()]
        self.assertIn("limit_released_for_conversion",
                      [event["event"] for event in events])
        self.assertNotIn("converted_market_opened",
                         [event["event"] for event in events])

    def test_a_safely_expired_or_cancelled_order_can_still_convert(self):
        for terminal_state in ("EXPIRED", "CANCELED", "REJECTED"):
            with self.subTest(terminal_state=terminal_state):
                broker = FakeBroker()
                plan = self._working_limit(broker)
                ticket = self.state.trades[plan.plan_id].pending_tickets[0]
                broker._orders.clear()
                broker.finished_orders[ticket] = terminal_state
                with redirect_stdout(io.StringIO()):
                    self.assertTrue(
                        trader.release_for_conversion(broker, self.state, plan))
                self.assertEqual(broker.cancelled, [])
                self.state.trades.clear()

    def test_a_disappeared_order_with_unknown_history_stays_blocked(self):
        broker = FakeBroker()
        plan = self._working_limit(broker)
        broker._orders.clear()
        with redirect_stdout(io.StringIO()):
            self.assertFalse(trader.release_for_conversion(broker, self.state, plan))

    def test_a_fill_before_the_initial_snapshot_stops_the_market_send(self):
        """A fill between pass-level sync and release must not hide in `before`."""
        broker = FakeBroker()
        plan = self._working_limit(broker)
        ticket = self.state.trades[plan.plan_id].pending_tickets[0]

        # The order is already gone and its position already exists when release
        # starts. A post-cancel position diff alone cannot detect this ordering.
        broker.fill_order(ticket)
        with redirect_stdout(io.StringIO()):
            self.assertFalse(trader.release_for_conversion(broker, self.state, plan))

        self.assertTrue(broker.positions())
        self.assertEqual(self.state.trades[plan.plan_id].pending_tickets, [ticket])
        self.assertFalse(self.state.trades[plan.plan_id].conversion_released)

    def test_an_already_released_conversion_is_not_retried(self):
        broker = FakeBroker()
        plan = self._working_limit(broker)
        with redirect_stdout(io.StringIO()):
            self.assertTrue(trader.release_for_conversion(broker, self.state, plan))
            self.assertFalse(trader.release_for_conversion(broker, self.state, plan))
        self.assertEqual(len(broker.cancelled), 1)

    def test_a_rejected_first_market_leg_is_not_retried_after_restart(self):
        """Replacing the pending record must not erase the durable release marker."""
        broker = FakeBroker()
        plan = self._working_limit(broker)
        with redirect_stdout(io.StringIO()):
            self.assertTrue(trader.release_for_conversion(broker, self.state, plan))
            # The working limit was the first recorded send; reject market leg 1.
            broker.reject_leg = len(broker.sent) + 1
            trade = trader.open_trade(
                broker, replace(Settings(), exit_mode="be_33_33_34"),
                self.state, plan, 50_000.0,
            )

        self.assertIsNotNone(trade)
        self.assertTrue(trade.closed)
        self.assertEqual(trade.position_tickets, [])
        self.assertTrue(trade.conversion_released)

        # Load the persisted state to model a process restart inside the same
        # conversion bar. It must retain the one-attempt decision.
        restarted = BotState.load(Path(trader.STATE_PATH))
        self.assertTrue(restarted.trades[plan.plan_id].conversion_released)
        with redirect_stdout(io.StringIO()):
            self.assertFalse(trader.release_for_conversion(broker, restarted, plan))

    def test_a_fill_landing_during_the_cancel_stops_the_market_send(self):
        """The race this guard exists for: the retrace prints as we give up on it."""

        class RacingBroker(FakeBroker):
            def cancel(self, ticket, reason):
                # The order filled a moment before the cancel reached the server.
                self.fill_order(ticket)
                raise OrderRejected("cancel rejected: retcode=10036 order already filled")

        broker = RacingBroker()
        plan = self._working_limit(broker)
        before = len(broker.positions())
        with redirect_stdout(io.StringIO()):
            self.assertFalse(trader.release_for_conversion(broker, self.state, plan))
        # The plan keeps its ticket so the normal fill-sync path still owns it.
        self.assertEqual(len(broker.positions()), before + 1)
        self.assertTrue(self.state.trades[plan.plan_id].pending_tickets)

    def test_a_silent_fill_is_caught_even_when_the_cancel_reports_success(self):
        """`order_send` returning done is not proof the order was not filled."""

        class SilentFillBroker(FakeBroker):
            def cancel(self, ticket, reason):
                self.cancelled.append((ticket, reason))
                self.fill_order(ticket)        # became a position, not a cancel
                return {"dry_run": False}

        broker = SilentFillBroker()
        plan = self._working_limit(broker)
        with redirect_stdout(io.StringIO()):
            self.assertFalse(trader.release_for_conversion(broker, self.state, plan))

    def test_a_fill_hidden_from_positions_is_caught_by_order_history(self):
        """MT5 may publish FILLED before the resulting position is visible."""

        class DelayedPositionBroker(FakeBroker):
            def cancel(self, ticket, reason):
                self.cancelled.append((ticket, reason))
                self._orders = [order for order in self._orders
                                if order["ticket"] != ticket]
                self.finished_orders[ticket] = "FILLED"
                return {"dry_run": False}

        broker = DelayedPositionBroker()
        plan = self._working_limit(broker)
        with redirect_stdout(io.StringIO()):
            self.assertFalse(trader.release_for_conversion(broker, self.state, plan))
        self.assertEqual(broker.positions(), [])
        self.assertTrue(self.state.trades[plan.plan_id].pending_tickets)

    def test_an_order_still_working_after_the_cancel_stops_the_market_send(self):
        class StubbornBroker(FakeBroker):
            def cancel(self, ticket, reason):
                self.cancelled.append((ticket, reason))
                return {"dry_run": False}      # accepted, but the order stays live

        broker = StubbornBroker()
        plan = self._working_limit(broker)
        with redirect_stdout(io.StringIO()):
            self.assertFalse(trader.release_for_conversion(broker, self.state, plan))
        self.assertTrue(broker.pending_orders())

    def test_a_plan_that_already_holds_a_position_is_never_converted(self):
        broker = FakeBroker()
        plan = self._working_limit(broker)
        self.state.trades[plan.plan_id].position_tickets.append(999)
        with redirect_stdout(io.StringIO()):
            self.assertFalse(trader.release_for_conversion(broker, self.state, plan))
        self.assertEqual(broker.cancelled, [])

    def test_a_plan_with_no_record_needs_no_release(self):
        """After a restart the bot may see the conversion with nothing working."""
        broker = FakeBroker()
        with redirect_stdout(io.StringIO()):
            self.assertTrue(trader.release_for_conversion(
                broker, self.state, replace(intent(), converted=True)))

    def test_a_missing_record_does_not_ignore_same_timeframe_broker_exposure(self):
        """A reset state file must not turn an orphan limit into double risk."""
        broker = FakeBroker()
        broker.limit_entry(1, 0.05, 3990.0, 3974.0, 4006.0, None, "M15 TP1 be33")
        with redirect_stdout(io.StringIO()):
            self.assertFalse(trader.release_for_conversion(
                broker, self.state, replace(intent(), converted=True)))

    def test_a_missing_record_ignores_another_timeframes_order(self):
        broker = FakeBroker()
        broker.limit_entry(1, 0.05, 3990.0, 3974.0, 4006.0, None, "M30 TP1 be33")
        with redirect_stdout(io.StringIO()):
            self.assertTrue(trader.release_for_conversion(
                broker, self.state, replace(intent(), converted=True)))

    def test_the_timeout_counts_from_the_fill_bar_not_the_signal_bar(self):
        """A converted plan fills bars later; anchoring to the signal retires it early."""
        broker = FakeBroker()
        plan = replace(intent(action="market"), converted=True, bars_since_signal=2,
                       fill_bar_time="2026-06-01 00:30:00")
        with redirect_stdout(io.StringIO()):
            trade = trader.open_trade(broker, replace(Settings(), exit_mode="be_33_33_34"),
                                      self.state, plan, 50_000.0)
        self.assertIsNotNone(trade)
        self.assertEqual(trade.fill_bar_time, "2026-06-01 00:30:00")

    def test_a_converted_market_entry_is_sized_for_worst_allowed_fill(self):
        class PriceLimitBroker(FakeBroker):
            def __init__(self):
                super().__init__()
                self.worst_prices = []

            def market_entry(self, direction, volume, stop, take_profit, comment,
                             worst_price=None):
                self.worst_prices.append(worst_price)
                return super().market_entry(direction, volume, stop, take_profit,
                                            comment, worst_price=worst_price)

        broker = PriceLimitBroker()
        plan = replace(intent(action="market"), converted=True,
                       fill_bar_time="2026-06-01 00:30:00")
        settings = replace(Settings(), exit_mode="be_33_33_34")
        with redirect_stdout(io.StringIO()):
            trade = trader.open_trade(broker, settings, self.state, plan, 50_000.0)

        worst_distance = trader.sizing_stop_distance(broker, settings, plan)
        worst_cash = sum(trade.legs) * worst_distance * broker.spec.value_per_point
        self.assertLessEqual(worst_cash, 50_000.0 * settings.risk_percent / 100)
        self.assertTrue(broker.worst_prices)
        self.assertTrue(all(price == 4002.4 for price in broker.worst_prices))
        events = [json.loads(line) for line in
                  Path(trader.JOURNAL_PATH).read_text(encoding="utf-8").splitlines()]
        self.assertIn("converted_market_opened", [event["event"] for event in events])

    def test_an_immediate_market_entry_is_also_sized_for_worst_allowed_fill(self):
        broker = FakeBroker()
        plan = intent(action="market")
        settings = replace(Settings(), exit_mode="be_33_33_34")
        with redirect_stdout(io.StringIO()):
            trade = trader.open_trade(broker, settings, self.state, plan, 50_000.0)

        worst_distance = trader.sizing_stop_distance(broker, settings, plan)
        worst_cash = sum(trade.legs) * worst_distance * broker.spec.value_per_point
        self.assertLessEqual(worst_cash, 50_000.0 * settings.risk_percent / 100)

    def test_an_ordinary_market_entry_still_anchors_to_its_signal_bar(self):
        broker = FakeBroker()
        with redirect_stdout(io.StringIO()):
            trade = trader.open_trade(broker, replace(Settings(), exit_mode="be_33_33_34"),
                                      self.state, intent(action="market"), 50_000.0)
        self.assertEqual(trade.fill_bar_time, "2026-06-01 00:00:00")

    def test_a_limit_expires_when_the_plan_would_convert_not_bars_later(self):
        """If the bot dies before converting, the order must not outlive the plan."""
        from xau import quantum

        original = quantum.CONVERT_TO_MARKET_BARS
        try:
            quantum.CONVERT_TO_MARKET_BARS = 2
            self.assertEqual(signals.limit_life_bars(), 2)
            quantum.CONVERT_TO_MARKET_BARS = None
            self.assertEqual(signals.limit_life_bars(), signals.ENTRY_TIMEOUT_BARS)
        finally:
            quantum.CONVERT_TO_MARKET_BARS = original


def replace_closures(settings, closures):
    from dataclasses import replace

    return replace(settings, market_closures=closures)
