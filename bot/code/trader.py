"""Turn a plan into orders and manage it until it is closed.

The production setting is ``capital_tier``, so which exit runs depends on the
account's anchored capital: ``fixed_tp3`` below 30,000 — one position, full exit
at TP3 (2R) — and ``be_33_33_34`` at or above it, which uses three broker-side
legs and moves the survivors to break-even after TP1. A 50,000 account is on the
split. Both carry the same 120-bar timeout the study measured.

The three legs are sent one at a time with `Broker.write_spacing_seconds`
between them. Firing them in the same instant reads as automated order flooding
and earns a temporary block, which is the one failure that can strand a
half-placed trade. Only the timing changes; entry, stop, targets and sizing are
still exactly what the plan asked for.
"""
from __future__ import annotations

import math
from datetime import timedelta

import pandas as pd

from xau import config
from xau.mt5_source import MT5Error

from . import journal, signals
from .broker import Broker, OrderRejected, Position
from .settings import JOURNAL_PATH, STATE_PATH, Settings
from .signals import Intent
from .sizing import SizingError, size_plan
from .state import BotState, ManagedTrade


def _timeframe_delta(timeframe: str) -> timedelta:
    return timedelta(seconds=config.TIMEFRAME_SECONDS[timeframe.upper()])


def _leg_targets(intent: Intent, leg_count: int, fallback_index: int) -> list[float]:
    if leg_count == 1:
        return [intent.targets[fallback_index]]
    return list(intent.targets[:leg_count])


def _position_target_index(trade: ManagedTrade, position: Position) -> int:
    """Match a broker position to its planned target, independent of ticket order."""
    if not trade.targets:
        return 999
    return min(
        range(len(trade.targets)),
        key=lambda index: abs(position.take_profit - trade.targets[index]),
    )


def _breakeven_cost_buffer(broker: Broker, position: Position | None = None) -> float:
    """Price distance needed to cover costs and current negative swap.

    The technique lab already charges commission and stop slippage that are
    absent from bar data. Live break-even must use the same cost model;
    otherwise a stop at the fill price still closes net-negative after fees.
    Spread is not added here: a BUY position's actual ``price_open`` is the ask
    and its stop executes against bid (vice versa for SELL), so the fill-to-exit
    price difference already contains the spread.
    """
    symbol_key = getattr(broker, "symbol_key", "XAUUSD").upper()
    costs = config.cost_cfg(symbol_key)
    value_per_point = float(broker.spec.value_per_point or 0.0)
    commission_price = (
        float(costs.get("commission_per_lot", 0.0)) / value_per_point
        if value_per_point > 0 else 0.0
    )
    try:
        source_point = 10 ** -int(config.symbol_cfg(symbol_key)["decimals"])
    except KeyError:
        source_point = float(broker.spec.point)
    slippage_price = float(costs.get("slippage_points", 0.0)) * source_point
    negative_swap_price = 0.0
    if position is not None and position.volume > 0 and value_per_point > 0:
        # POSITION_SWAP is cumulative cash in the account currency. Positive
        # swap is deliberately ignored: a future rate change must never loosen
        # a stop that has already been tightened.
        negative_swap_cash = max(0.0, -float(getattr(position, "swap", 0.0)))
        negative_swap_price = (
            negative_swap_cash / (value_per_point * position.volume)
        )
    return max(0.0, commission_price + slippage_price + negative_swap_price)


def breakeven_stop(broker: Broker, position: Position) -> float:
    """Cost-covered break-even based on this leg's authoritative broker fill."""
    raw = (
        position.price_open
        + position.direction * _breakeven_cost_buffer(broker, position)
    )
    scale = 10 ** broker.spec.digits
    # Round away from the losing side. Built-in round() can round a BUY stop
    # down (or a SELL stop up), silently discarding the last cost-covering tick.
    if position.direction == 1:
        return math.ceil(raw * scale - 1e-9) / scale
    return math.floor(raw * scale + 1e-9) / scale


def _check_exit_mode(exit_mode: str, sizing, intent: Intent) -> str | None:
    """Reason to refuse the trade because it cannot honour `exit_mode`, or None.

    `be_33_33_34` on a balance too small to split would quietly become a single
    leg — the exact silent swap `exit_mode` exists to prevent. Refusing is the
    honest answer: the operator asked for an exit the account cannot place.
    """
    if exit_mode == "be_33_33_34" and sizing.single_leg:
        return (f"exit_mode=be_33_33_34 needs three legs but the balance only "
                f"sizes {sizing.legs}; raise the balance or set exit_mode=fixed_tp3")
    return None


def open_trade(broker: Broker, settings: Settings, state: BotState, intent: Intent,
               balance: float) -> ManagedTrade | None:
    """Size the plan and place its legs. Returns None when nothing was sent."""
    # Never let current balance/equity flip the capital tier. `initial_balance`
    # is durable state captured on the first run; using it here as well as in the
    # caller makes the invariant survive refactors and direct calls.
    risk_basis = ((state.initial_balance or balance)
                  if settings.exit_mode == "capital_tier" else balance)
    exit_mode = settings.resolved_exit_mode(risk_basis)
    leg_weights = settings.leg_weights_for(risk_basis)
    try:
        sizing = size_plan(broker.spec, risk_basis, settings.risk_percent,
                           intent.risk, leg_weights,
                           rounding=settings.lot_rounding,
                           max_overshoot=settings.max_risk_overshoot)
    except SizingError as error:
        journal.write(JOURNAL_PATH, "sizing_rejected", plan_id=intent.plan_id,
                      reason=str(error))
        print(f"[SIZING_REJECTED] plan={intent.plan_id} reason={str(error)!r}")
        return None

    # The lot step can only round down, so the position often carries less risk
    # than the settings asked for. Say so when the gap is wide enough to matter:
    # every R in the journal is measured against the real number, and a silent
    # half-size position would read as a system with twice the edge it has.
    if sizing.risk_shortfall < 0.85:
        journal.write(JOURNAL_PATH, "risk_rounded_down", plan_id=intent.plan_id,
                      intended=round(sizing.intended_risk_cash, 2),
                      actual=round(sizing.risk_cash, 2),
                      fraction=round(sizing.risk_shortfall, 3),
                      lots=list(sizing.legs))
        print(f"[RISK_ROUNDED] plan={intent.plan_id} "
              f"intended={sizing.intended_risk_cash:.2f} "
              f"actual={sizing.risk_cash:.2f} "
              f"({sizing.risk_shortfall:.0%} of target) lots={sizing.legs}")

    refusal = _check_exit_mode(exit_mode, sizing, intent)
    if refusal is not None:
        journal.write(JOURNAL_PATH, "exit_mode_rejected", plan_id=intent.plan_id,
                      reason=refusal, exit_mode=exit_mode)
        print(f"[SIZING_REJECTED] plan={intent.plan_id} reason={refusal!r}")
        return None

    # Resolve legacy `auto` into the policy that was actually executable so
    # state, journal and MT5 comments never claim a split that was not sent.
    actual_exit_mode = (
        "fixed_tp3" if exit_mode == "auto" and sizing.single_leg
        else "be_33_33_34" if exit_mode == "auto"
        else exit_mode
    )
    targets = _leg_targets(intent, len(sizing.legs), settings.single_leg_fallback_target)
    trade = ManagedTrade(
        plan_id=intent.plan_id, timeframe=intent.timeframe, direction=intent.direction,
        entry=intent.entry, stop=intent.stop, risk=intent.risk,
        risk_cash=round(sizing.risk_cash, 2), targets=targets, legs=list(sizing.legs),
        dry_run=broker.dry_run, exit_mode=actual_exit_mode,
    )

    expiry = None
    if intent.action == "limit":
        remaining = max(signals.ENTRY_TIMEOUT_BARS - intent.bars_since_signal, 1)
        expiry = broker.tick()["server_time"] + remaining * _timeframe_delta(intent.timeframe)

    # What is live *before* the first leg goes out. This used to be assembled
    # from `state.trades`, which silently assumed every live position had a local
    # record. An orphan — state.json reset, a crash mid-placement — has none, so
    # it failed the `not in known` test below and was adopted into this trade:
    # its P/L scored against this trade's `risk_cash`, its stop moved to this
    # trade's entry, and it disappeared from `enforce_orphan_timeout`, the one
    # thing that would have closed it. Asking the broker costs one request and
    # cannot be wrong: anything already open is not a leg we just sent.
    known = {position.ticket for position in broker.positions()}

    # Register the trade *before* the first order goes out. A leg can be rejected
    # halfway through — bad stops, margin, a requote — and if the plan were only
    # recorded on success, whatever did fill would be a position no part of this
    # bot knows about: no break-even, no timeout, missing from the journal, and on
    # the pending path the plan would look unseen and be sent again next bar.
    state.trades[trade.plan_id] = trade
    state.remember_plan(trade.plan_id)
    state.save(STATE_PATH)

    placed = 0
    try:
        for index, (volume, target) in enumerate(zip(sizing.legs, targets), start=1):
            # Name the target the leg is actually aimed at. `index` is the leg
            # number, which only equals the TP number when the position splits
            # three ways. Under `fixed_tp3` there is one leg going to TP3, and
            # labelling it "TP1" made every order in the terminal and in FTMO's
            # trade log claim an exit the bot was not taking.
            tp_number = (settings.single_leg_fallback_target + 1
                         if sizing.single_leg else index)
            mode_tag = "fixedtp3" if actual_exit_mode == "fixed_tp3" else "be33"
            tags = tuple(tag for tag in settings.tags
                         if tag not in {"fixedtp3", "be33"}) + (mode_tag,)
            comment = f"{intent.timeframe} TP{tp_number} {'|'.join(tags)}"
            if intent.action == "market":
                result = broker.market_entry(
                    intent.direction, volume, intent.stop, target, comment,
                )
                order_ticket = int(result.get("order") or 0)
                if order_ticket:
                    trade.market_order_tickets.append(order_ticket)
                    if index == 1 and actual_exit_mode == "be_33_33_34":
                        trade.tp1_market_order_ticket = order_ticket
                    # Persist each accepted leg immediately. The final broker
                    # position lookup below is still authoritative, but a hard
                    # power loss before `finally` now leaves enough identity for
                    # startup deal-history recovery.
                    state.save(STATE_PATH)
            else:
                result = broker.limit_entry(intent.direction, volume, intent.entry,
                                            intent.stop, target, expiry, comment)
                ticket = int(result.get("order") or 0)
                if ticket:
                    trade.pending_tickets.append(ticket)
                    if index == 1 and actual_exit_mode == "be_33_33_34":
                        trade.tp1_pending_ticket = ticket
                    state.save(STATE_PATH)
            placed = index
    except OrderRejected as error:
        journal.write(JOURNAL_PATH, "leg_rejected", plan_id=trade.plan_id,
                      placed=placed, of=len(sizing.legs), reason=str(error))
        print(f"[ORDER_REJECTED] plan={trade.plan_id} leg={placed + 1} "
              f"reason={str(error)!r} managed_legs={placed}")
    finally:
        # Adopt whatever actually reached the broker, however the loop ended, so
        # the exit machinery owns every live leg.
        if intent.action == "market":
            trade.filled_at = str(broker.tick()["server_time"])
            trade.fill_bar_time = intent.signal_time
            # Read the tickets back rather than trusting the order id to equal
            # the position id — brokers do not all agree on that.
            opened = [
                position for position in broker.positions()
                if position.ticket not in known
            ]
            opened.sort(key=lambda position: (
                _position_target_index(trade, position), position.ticket,
            ))
            trade.position_tickets = [position.ticket for position in opened]
            if opened and actual_exit_mode == "be_33_33_34":
                tp1 = min(opened, key=lambda position: (
                    _position_target_index(trade, position), position.ticket,
                ))
                if _position_target_index(trade, tp1) == 0:
                    trade.tp1_position_ticket = tp1.ticket
            # Every accepted market leg is visible, so no history recovery is
            # outstanding. If visibility lags, retain the order ids for startup.
            if len(opened) >= placed:
                trade.market_order_tickets.clear()
            if trade.position_tickets:
                state.count_trading_day()
        if (not trade.position_tickets and not trade.pending_tickets
                and not trade.market_order_tickets):
            # Nothing is live, so nothing needs managing. It stays in `trades` as
            # a closed record, which also stops the plan being retried. Accepted
            # market order ids are live uncertainty, not "nothing": MT5 can make
            # the order visible in deal history a moment before its position is
            # returned by positions_get().
            trade.closed = True
        state.save(STATE_PATH)

    journal.write(JOURNAL_PATH, "trade_opened", plan_id=trade.plan_id,
                  side=intent.side, action=intent.action, timeframe=intent.timeframe,
                  entry=intent.entry, stop=intent.stop, risk=intent.risk,
                  targets=targets, legs=list(sizing.legs), legs_placed=placed,
                  risk_cash=trade.risk_cash,
                  single_leg=sizing.single_leg, exit_mode=actual_exit_mode,
                  dry_run=broker.dry_run)
    if trade.position_tickets:
        print(f"[POSITION_OPENED] plan={intent.plan_id} side={intent.side} "
              f"positions={trade.position_tickets} volumes={list(sizing.legs)} "
              f"entry={intent.entry} sl={intent.stop} targets={targets} "
              f"risk_cash={trade.risk_cash:.2f}")
    elif trade.pending_tickets:
        expiry_text = expiry.strftime("%Y-%m-%d %H:%M:%S") if expiry else "GTC"
        print(f"[PENDING_CREATED] plan={intent.plan_id} side={intent.side} "
              f"orders={trade.pending_tickets} volumes={list(sizing.legs)} "
              f"entry={intent.entry} sl={intent.stop} targets={targets} "
              f"expires={expiry_text} risk_cash={trade.risk_cash:.2f}")
    elif trade.market_order_tickets:
        print(f"[MARKET_SYNC_WAIT] plan={intent.plan_id} "
              f"orders={trade.market_order_tickets} "
              "reason='orders accepted; waiting for MT5 position visibility'")
    else:
        print(f"[ORDER_NOT_OPENED] plan={intent.plan_id} reason='no live ticket returned'")
    return trade


def sync_fills(broker: Broker, state: BotState,
               frames: dict[str, pd.DataFrame] | None = None) -> None:
    """Reconcile pending orders with their resulting MT5 position identifiers.

    MT5 order tickets and position tickets are different identifiers on many
    brokers. Deal history provides the authoritative order-to-position mapping.
    """
    live_orders = {order["ticket"] for order in broker.pending_orders()}
    pending_tickets = [
        ticket
        for trade in state.open_trades()
        for ticket in trade.pending_tickets
    ]
    market_order_tickets = [
        ticket
        for trade in state.open_trades()
        for ticket in trade.market_order_tickets
    ]
    missing_tickets = [ticket for ticket in pending_tickets if ticket not in live_orders]
    history_tickets = list(dict.fromkeys(missing_tickets + market_order_tickets))
    filled_by_order = broker.filled_order_positions(history_tickets)
    finished_states = broker.finished_order_states(missing_tickets)
    terminal_states = {"CANCELED", "REJECTED", "EXPIRED"}
    changed = False

    for trade in state.open_trades():
        original_market = list(trade.market_order_tickets)
        recovered_market = [
            ticket for ticket in original_market if ticket in filled_by_order
        ]
        for order_ticket in recovered_market:
            position_ticket = filled_by_order[order_ticket]
            if position_ticket not in trade.position_tickets:
                trade.position_tickets.append(position_ticket)
            if order_ticket == trade.tp1_market_order_ticket:
                trade.tp1_position_ticket = position_ticket
        trade.market_order_tickets = [
            ticket for ticket in original_market if ticket not in filled_by_order
        ]
        unresolved_market = list(trade.market_order_tickets)
        if recovered_market:
            changed = True
            trade.filled_at = trade.filled_at or str(broker.tick()["server_time"])
            if not trade.fill_bar_time:
                trade.fill_bar_time = _fill_bar_time(frames, trade, broker)
            state.count_trading_day()
            recovered_positions = [
                filled_by_order[ticket] for ticket in recovered_market
            ]
            journal.write(
                JOURNAL_PATH, "market_fill_recovered",
                plan_id=trade.plan_id, order_tickets=recovered_market,
                position_tickets=recovered_positions,
                fill_bar_time=trade.fill_bar_time,
            )
            print(f"[MARKET_FILL_RECOVERED] plan={trade.plan_id} "
                  f"orders={recovered_market} positions={recovered_positions}")
        if unresolved_market:
            print(f"[MARKET_SYNC_WAIT] plan={trade.plan_id} "
                  f"orders={unresolved_market} "
                  "reason='waiting for MT5 deal history'")

        if not trade.pending_tickets:
            continue
        original = list(trade.pending_tickets)
        missing = [ticket for ticket in original if ticket not in live_orders]
        filled_orders = [ticket for ticket in missing if ticket in filled_by_order]
        removed_orders = [ticket for ticket in missing
                          if finished_states.get(ticket) in terminal_states]
        unresolved_orders = [ticket for ticket in missing
                             if ticket not in filled_by_order
                             and ticket not in removed_orders]
        filled_positions = [filled_by_order[ticket] for ticket in filled_orders]

        trade.pending_tickets = [ticket for ticket in original
                                 if ticket in live_orders or ticket in unresolved_orders]
        for order_ticket, position_ticket in zip(filled_orders, filled_positions):
            if position_ticket not in trade.position_tickets:
                trade.position_tickets.append(position_ticket)
            if order_ticket == trade.tp1_pending_ticket:
                trade.tp1_position_ticket = position_ticket
        changed = changed or trade.pending_tickets != original

        if filled_positions:
            trade.filled_at = trade.filled_at or str(broker.tick()["server_time"])
            if not trade.fill_bar_time:
                trade.fill_bar_time = _fill_bar_time(frames, trade, broker)
            state.count_trading_day()
            journal.write(JOURNAL_PATH, "pending_filled", plan_id=trade.plan_id,
                          order_tickets=filled_orders,
                          position_tickets=filled_positions,
                          fill_bar_time=trade.fill_bar_time)
            print(f"[PENDING_FILLED] plan={trade.plan_id} "
                  f"orders={filled_orders} positions={filled_positions}")

        if removed_orders:
            states = {ticket: finished_states[ticket] for ticket in removed_orders}
            journal.write(JOURNAL_PATH, "pending_removed", plan_id=trade.plan_id,
                          order_tickets=removed_orders, states=states)
            print(f"[PENDING_REMOVED] plan={trade.plan_id} states={states}")

        if unresolved_orders:
            print(f"[PENDING_SYNC_WAIT] plan={trade.plan_id} orders={unresolved_orders} "
                  "reason='waiting for MT5 order/deal history'")

        if (not trade.pending_tickets and not trade.position_tickets
                and not trade.market_order_tickets):
            trade.closed = True
            changed = True

    if changed:
        state.save(STATE_PATH)

def _fill_bar_time(frames: dict[str, pd.DataFrame] | None, trade: ManagedTrade,
                   broker: Broker) -> str:
    """The closed bar the fill belongs to, which starts the 120-bar clock.

    The strategy counts from the bar that filled, so the last closed bar on the
    trade's own timeframe is the right anchor. Falling back to the raw server
    time still beats leaving it unset: a timestamp `bars_since` cannot match
    yields 0 bars, which merely delays the timeout instead of disabling it.
    """
    frame = (frames or {}).get(trade.timeframe)
    if frame is not None and len(frame):
        return str(pd.Timestamp(frame["time"].iloc[-1]))
    return str(broker.tick()["server_time"])


def apply_breakeven(broker: Broker, state: BotState) -> set[int]:
    """Move survivors to cost-covered fills and return live position tickets.

    All legs share one stop, so if the TP1 leg is gone while later legs are still
    open, TP1 must have been taken rather than stopped out.

    A `fixed_tp3` trade holds one leg and is skipped by the `< 2` test below,
    which is what should happen: there is no TP1 to bank and nothing behind it to
    protect, and moving that single stop to entry would exit at break-even a
    trade the measured policy carries all the way to TP3.
    """
    open_positions = {position.ticket: position for position in broker.positions()}
    # A dry-run Broker reports the account's real positions but simulates every
    # write. Marking those positions protected would corrupt durable live state
    # even though no modification reached MT5. Still return their tickets: the
    # fast management loop uses this snapshot to decide whether a trade closed,
    # and an empty result would make live positions look finished.
    if broker.dry_run:
        return set(open_positions)

    for trade in state.open_trades():
        if trade.exit_mode != "be_33_33_34" or len(trade.position_tickets) < 2:
            continue
        if trade.tp1_position_ticket is not None:
            tp1_ticket = trade.tp1_position_ticket
        elif (trade.tp1_market_order_ticket is not None
              or trade.tp1_pending_ticket is not None):
            # New records know which exact order belongs to TP1. If that order's
            # deal has not appeared in history yet (or the order was canceled),
            # guessing that the first mapped TP2/TP3 position is TP1 can move a
            # survivor's stop without TP1 ever being banked. Wait for the
            # authoritative order -> position mapping instead.
            continue
        else:
            # Legacy state predates explicit TP1 identity. Its original contract
            # stored market legs in TP order, so list order is the only recovery
            # information available.
            tp1_ticket = trade.position_tickets[0]
        if tp1_ticket in open_positions:
            continue
        survivors = [
            open_positions[ticket]
            for ticket in trade.position_tickets
            if ticket != tp1_ticket and ticket in open_positions
        ]
        if not survivors:
            continue
        moved = []
        rejected = []
        already_protected = []
        desired_stops = {}
        cost_buffers = {}
        swaps = {}
        for position in survivors:
            desired_stop = breakeven_stop(broker, position)
            desired_stops[position.ticket] = desired_stop
            cost_buffers[position.ticket] = round(
                _breakeven_cost_buffer(broker, position), broker.spec.digits,
            )
            swaps[position.ticket] = float(getattr(position, "swap", 0.0))
            # MT5 uses 0.0 for "no stop". For a SELL, the normal comparison
            # (entry < stop) is false against zero and used to misclassify a
            # completely unprotected survivor as already beyond break-even.
            improves = (
                not position.stop
                or (desired_stop > position.stop if trade.direction == 1
                    else desired_stop < position.stop)
            )
            if improves:
                try:
                    broker.move_stop(position, desired_stop)
                except OrderRejected as error:
                    journal.write(JOURNAL_PATH, "breakeven_rejected",
                                  ticket=position.ticket, reason=str(error))
                    print(f"[STOP_REJECTED] ticket={position.ticket} "
                          f"reason={str(error)!r} scope='break_even'")
                    rejected.append(position.ticket)
                    continue
                moved.append(position)
            else:
                # A previous process may have moved this leg at the broker and
                # crashed before persisting `breakeven_done`. Treat a stop at or
                # beyond entry as protected so startup reconciliation is
                # idempotent and can repair the local state without another
                # broker write.
                already_protected.append(position.ticket)

        # Do not claim the transition is complete when only some survivor legs
        # accepted the modification. Leaving the flag false makes the next
        # management pass retry only the legs that are still below break-even.
        if rejected:
            # Older versions could persist True after only one survivor moved.
            # Clear that stale flag so fast split polling becomes active again.
            trade.breakeven_done = False
            continue
        if trade.breakeven_done and not moved:
            continue
        trade.breakeven_done = True
        journal.write(JOURNAL_PATH, "breakeven", plan_id=trade.plan_id,
                      tickets=[position.ticket for position in survivors],
                      moved_tickets=[position.ticket for position in moved],
                      already_protected=already_protected,
                      stops=desired_stops,
                      cost_buffers=cost_buffers,
                      swaps=swaps)
        if moved:
            print(f"[STOP_MOVED] plan={trade.plan_id} mode=NET_BREAK_EVEN "
                  f"stops={desired_stops} "
                  f"positions={[position.ticket for position in moved]}")
        else:
            print(f"[STOP_CONFIRMED] plan={trade.plan_id} mode=NET_BREAK_EVEN "
                  f"stops={desired_stops} positions={already_protected}")
    return set(open_positions)


def _orphan_timeframe(comment: str, frames: dict[str, pd.DataFrame]) -> str | None:
    """The timeframe an unmanaged position belongs to, read off its comment.

    `open_trade` writes "M15 TP3 quantum|be33", so the timeframe survives in MT5
    even when the local record does not. When the comment is unreadable — hand
    edited, truncated by a broker, written by an older build — fall back to the
    slowest configured timeframe. That is deliberately the conservative choice:
    120 M30 bars is twice the patience of 120 M15 bars, so guessing wrong delays
    the close rather than cutting a position short of the measured hold.
    """
    if not frames:
        return None
    head = comment.split()[0].upper() if comment.split() else ""
    if head in frames:
        return head
    return max(frames, key=lambda tf: config.TIMEFRAME_SECONDS[tf.upper()])


def enforce_orphan_timeout(broker: Broker, state: BotState,
                           frames: dict[str, pd.DataFrame]) -> None:
    """Time out the bot's own positions that no managed trade owns.

    `enforce_timeout` walks `state.open_trades()`, so a position whose record was
    lost — state.json reset, a crash before the trade was written, a record
    already marked closed while the position lived on — was held forever: no
    120-bar close, and the only sign of it was an `UNTRACKED_POSITION` alert
    nobody may be reading. The clock here runs from `opened_at`, the one
    timestamp such a position still carries.

    Scope is the same as everywhere else in this module: `broker.positions()`
    only returns this bot's magic, so a position opened by hand or by another EA
    is never touched.
    """
    managed = {ticket for trade in state.open_trades()
               for ticket in trade.position_tickets}
    for position in broker.positions():
        if position.ticket in managed:
            continue
        timeframe = _orphan_timeframe(position.comment, frames)
        frame = frames.get(timeframe) if timeframe else None
        if frame is None or not len(frame):
            continue
        bars = signals.bars_since_moment(frame, position.opened_at)
        if bars <= signals.TRADE_TIMEOUT_BARS:
            continue
        # An orphan is the position this bot knows least about, so it is also the
        # likeliest to be refused — a holiday session, a symbol gone read-only, a
        # ticket the broker has already retired. Letting that escape would abort
        # the rest of the pass and surface as `CONNECTION_LOST`, whose remedy is
        # a reconnect that cannot fix a rejection. Record it and carry on: the
        # next pass tries again, and the other orphans still get their close.
        try:
            broker.close(position, "timeout 120 bars")
        except OrderRejected as error:
            journal.write(JOURNAL_PATH, "orphan_timeout_rejected",
                          ticket=position.ticket, reason=str(error))
            print(f"[CLOSE_REJECTED] ticket={position.ticket} "
                  f"reason={str(error)!r} scope='unmanaged position'")
            continue
        journal.write(JOURNAL_PATH, "orphan_timeout_closed", ticket=position.ticket,
                      timeframe=timeframe, bars=bars,
                      opened_at=str(position.opened_at), comment=position.comment)
        print(f"[CLOSE_SUBMITTED] ticket={position.ticket} reason='timeout_120_bars' "
              f"scope='unmanaged position' timeframe={timeframe} bars={bars} "
              f"opened={position.opened_at:%Y-%m-%d %H:%M:%S}")


def enforce_timeout(broker: Broker, state: BotState, frames: dict[str, pd.DataFrame]) -> None:
    """Close what the backtest would have timed out at 120 bars."""
    open_positions = {position.ticket: position for position in broker.positions()}
    for trade in state.open_trades():
        frame = frames.get(trade.timeframe)
        if frame is None or not trade.fill_bar_time:
            continue
        if signals.bars_since(frame, trade.fill_bar_time) <= signals.TRADE_TIMEOUT_BARS:
            continue
        live = [open_positions[ticket] for ticket in trade.position_tickets
                if ticket in open_positions]
        if not live:
            continue    # already gone; `reconcile_closed` will score it
        closed = []
        for position in live:
            # A refused close used to escape and leave the later legs of this
            # timeout pass unattempted, even though each could still close.
            try:
                broker.close(position, "timeout 120 bars")
            except OrderRejected as error:
                journal.write(JOURNAL_PATH, "timeout_close_rejected",
                              ticket=position.ticket, reason=str(error))
                print(f"[CLOSE_REJECTED] ticket={position.ticket} "
                      f"reason={str(error)!r} scope='timeout_120_bars'")
                continue
            closed.append(position)
        if closed:
            journal.write(JOURNAL_PATH, "timeout_closed", plan_id=trade.plan_id,
                          tickets=[position.ticket for position in closed])
            print(f"[CLOSE_SUBMITTED] plan={trade.plan_id} reason='timeout_120_bars' "
                  f"positions={[position.ticket for position in closed]}")


def cancel_stale(broker: Broker, state: BotState, intent: Intent) -> None:
    """Drop a working order whose plan the strategy has abandoned."""
    trade = state.trades.get(intent.plan_id)
    if trade is None or not trade.pending_tickets:
        return
    live = {order["ticket"] for order in broker.pending_orders()}
    remaining = []
    for ticket in list(trade.pending_tickets):
        if ticket in live:
            # A refused cancel used to be forgotten below, leaving a working
            # order unowned until it filled or the orphan path eventually saw it.
            try:
                broker.cancel(ticket, intent.status)
            except OrderRejected as error:
                journal.write(JOURNAL_PATH, "cancel_rejected",
                              ticket=ticket, reason=str(error))
                print(f"[CANCEL_REJECTED] ticket={ticket} "
                      f"reason={str(error)!r} scope='stale_pending'")
                remaining.append(ticket)
    trade.pending_tickets[:] = remaining
    if (not trade.pending_tickets and not trade.position_tickets
            and not trade.market_order_tickets):
        trade.closed = True
    if remaining:
        print(f"[PENDING_CANCEL_INCOMPLETE] plan={trade.plan_id} tickets={remaining} "
              f"reason={intent.status!r}")
    else:
        journal.write(JOURNAL_PATH, "pending_cancelled", plan_id=trade.plan_id,
                      reason=intent.status)
        print(f"[PENDING_CANCELLED] plan={trade.plan_id} reason={intent.status!r}")


def reconcile_closed(broker: Broker, state: BotState) -> list[dict]:
    """Score finished trades in R and update the loss streak."""
    open_tickets = {position.ticket for position in broker.positions()}
    pending_tickets = {order["ticket"] for order in broker.pending_orders()}
    finished = []
    for trade in state.open_trades():
        if trade.dry_run or not trade.position_tickets:
            continue
        # A partial history response can map TP1 before the survivor market
        # orders. If TP1 is already gone, its closing deal is enough to make the
        # mapped subset look finished, but it is not enough to score and close
        # the whole setup while survivor order ids are still unresolved.
        if trade.market_order_tickets:
            print(f"[RECONCILE_WAIT] plan={trade.plan_id} "
                  f"market_orders={trade.market_order_tickets} "
                  "reason='market ticket reconciliation is incomplete'")
            continue
        if any(ticket in open_tickets for ticket in trade.position_tickets):
            continue
        if any(ticket in pending_tickets for ticket in trade.pending_tickets):
            continue
        since = pd.Timestamp(trade.filled_at or broker.tick()["server_time"]).to_pydatetime()
        deals = [deal for deal in broker.closed_deals(since - timedelta(days=2))
                 if deal["position"] in trade.position_tickets]
        # `net` is profit plus commission plus swap. Gold on M30 can be held for
        # 60 hours, so swap is not a rounding error, and R is the number that
        # decides whether this edge survived contact with a real broker.
        if not deals:
            print(f"[RECONCILE_WAIT] plan={trade.plan_id} "
                  "reason='position disappeared but deal history is not available yet'")
            continue
        profit = sum(deal["net"] for deal in deals)
        costs = sum(
            deal["commission"] + deal["swap"] + deal.get("fee", 0.0)
            for deal in deals
        )
        r_value = profit / trade.risk_cash if trade.risk_cash else None
        trade.closed = True
        state.day_realised += profit
        if profit < 0:
            state.consecutive_losses += 1
        elif profit > 0:
            state.consecutive_losses = 0
        record = journal.write(JOURNAL_PATH, "trade_closed", plan_id=trade.plan_id,
                               timeframe=trade.timeframe, profit=round(profit, 2),
                               costs=round(costs, 2),
                               r=round(r_value, 4) if r_value is not None else None,
                               breakeven_done=trade.breakeven_done)
        finished.append(record)
        scored = f" ({r_value:+.2f}R)" if r_value is not None else ""
        print(f"[POSITION_CLOSED] plan={trade.plan_id} net={profit:+.2f} "
              f"result={scored.strip() or 'n/a'} costs={costs:+.2f} "
              f"consecutive_losses={state.consecutive_losses}")
    return finished


def flatten_all(broker: Broker, state: BotState, reason: str) -> None:
    """Emergency exit: cancel everything working and close every position."""
    refused = []
    for order in broker.pending_orders():
        ticket = order["ticket"]
        # One refusal used to abort the emergency exit and report a flat account
        # while every later order and position was still exposed.
        try:
            broker.cancel(ticket, reason)
        except OrderRejected as error:
            refused.append(ticket)
            journal.write(JOURNAL_PATH, "flatten_rejected",
                          ticket=ticket, reason=str(error))
            print(f"[CANCEL_REJECTED] ticket={ticket} "
                  f"reason={str(error)!r} scope='emergency_flatten'")
    for position in broker.positions():
        try:
            broker.close(position, reason)
        except OrderRejected as error:
            refused.append(position.ticket)
            journal.write(JOURNAL_PATH, "flatten_rejected",
                          ticket=position.ticket, reason=str(error))
            print(f"[CLOSE_REJECTED] ticket={position.ticket} "
                  f"reason={str(error)!r} scope='emergency_flatten'")
    for trade in state.open_trades():
        trade.pending_tickets[:] = [ticket for ticket in trade.pending_tickets
                                    if ticket in refused]
    journal.write(JOURNAL_PATH, "flatten_all", reason=reason, refused=refused)
    if refused:
        print(f"[EMERGENCY_FLATTEN] incomplete; refused={refused} | reason={reason!r}")
    else:
        print(f"[EMERGENCY_FLATTEN] submitted for every order and position | reason={reason!r}")


def prune(state: BotState, keep: int = 200) -> None:
    """Keep state.json small without losing anything still working."""
    closed = [plan_id for plan_id, trade in state.trades.items() if trade.closed]
    for plan_id in closed[:-keep] if len(closed) > keep else []:
        state.trades.pop(plan_id, None)


def describe(position: Position) -> str:
    swap = float(getattr(position, "swap", 0.0))
    gross_plus_swap = position.profit + swap
    return (f"ticket={position.ticket} side={'BUY' if position.direction == 1 else 'SELL'} "
            f"volume={position.volume:g} entry={position.price_open} "
            f"sl={position.stop} tp={position.take_profit} "
            f"gross={position.profit:+.2f} swap={swap:+.2f} "
            f"gross_plus_swap={gross_plus_swap:+.2f} "
            f"opened={position.opened_at:%Y-%m-%d %H:%M:%S} "
            f"comment={position.comment!r}")
