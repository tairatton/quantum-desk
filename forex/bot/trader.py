"""Turn a plan into orders and manage it until it is closed.

The production setting is ``capital_tier``, so which exit runs depends on the
account's anchored capital: ``fixed_tp3`` below 30,000 — one position, full exit
at TP3 (2R) — and ``be_33_33_34`` at or above it, which uses three broker-side
legs and moves the survivors to break-even after TP1, then locks the TP3
survivor at the TP1 level after TP2. A 50,000 account is on the split. Both
carry the same 120-bar timeout the study measured.

The three legs are sent one at a time with `Broker.write_spacing_seconds`
between them. Firing them in the same instant reads as automated order flooding
and earns a temporary block, which is the one failure that can strand a
half-placed trade. Only the timing changes; entry, stop, targets and sizing are
still exactly what the plan asked for.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta

import pandas as pd

from strategy import config
from strategy.mt5_source import MT5Error

from engine import journal, signals
from .broker import Broker, OrderRejected, Position
from .settings import JOURNAL_PATH, STATE_PATH, Settings
from engine.signals import Intent
from engine.sizing import SizingError, size_plan
from engine.state import BotState, ManagedTrade, ftmo_day


_RECOVERABLE_COMMENT = re.compile(
    r"^(?P<timeframe>M\d+)\s+TP(?P<target>[123])\b.*\bbe33\b",
    re.IGNORECASE,
)


def _timeframe_delta(timeframe: str) -> timedelta:
    return timedelta(seconds=config.TIMEFRAME_SECONDS[timeframe.upper()])


def sizing_stop_distance(broker: Broker, settings: Settings, intent: Intent) -> float:
    """Conservative stop distance used to size an order before its fill exists.

    Market entries may legally be sent up to `max_entry_slippage_r` beyond the
    bar close, and MT5 may fill another `deviation_points` past the requested
    quote. Sizing them on the plan's original R would therefore exceed the cash
    risk setting precisely when entry is worst. This applies to immediate and
    converted market entries alike.
    """
    if intent.action != "market":
        return intent.risk
    allowed_adverse = intent.risk * settings.max_entry_slippage_r
    execution_deviation = settings.deviation_points * broker.spec.point
    return intent.risk + allowed_adverse + execution_deviation


def _positions_risk_cash(broker: Broker, positions, stop: float | None = None) -> float:
    """Cash the supplied fills can lose at the plan's original stop."""
    total = 0.0
    for position in positions:
        stop_price = position.stop if stop is None else stop
        distance = (position.price_open - stop_price) * position.direction
        if distance > 0:
            total += distance * broker.spec.value_per_point * position.volume
    return total


def _count_trading_moment(state: BotState, moment: datetime) -> None:
    """Attribute an entry to its real FTMO day, not the restart day."""
    if state.server_utc_offset is None:
        state.count_trading_day()
        return
    key = ftmo_day(moment, state.server_utc_offset).isoformat()
    if key not in state.trading_days:
        state.trading_days.append(key)


def _recovered_fill_moment(broker: Broker, position_tickets: list[int],
                           frames: dict[str, pd.DataFrame] | None = None,
                           timeframe: str = "") -> datetime:
    """Best authoritative fill time for positions recovered through history."""
    history_reader = getattr(broker, "filled_position_time", None)
    moments = ([history_reader(ticket) for ticket in position_tickets]
               if history_reader is not None else [])
    moments = [moment for moment in moments if moment is not None]
    frame = (frames or {}).get(timeframe)
    if not moments and frame is not None and len(frame):
        # Legacy/test adapters have no deal-time cache. The last closed bar was
        # the pre-existing fallback and remains safer than an unrelated local
        # wall-clock timestamp.
        return pd.Timestamp(frame["time"].iloc[-1]).to_pydatetime()
    if not moments:
        live = {position.ticket: position for position in broker.positions()}
        moments = [live[ticket].opened_at for ticket in position_tickets
                   if ticket in live]
    return min(moments) if moments else broker.tick()["server_time"]


def _actualize_converted_risk_from_deals(broker: Broker, trade: ManagedTrade,
                                         deals: list[dict]) -> bool:
    """Replace conservative conversion risk once entry history is complete."""
    if not trade.converted_risk_pending or not trade.position_tickets:
        return False
    wanted = set(trade.position_tickets)
    entries = [
        deal for deal in deals
        if deal.get("position") in wanted and deal.get("is_exit") is False
    ]
    covered = {int(deal["position"]) for deal in entries}
    if not wanted.issubset(covered):
        return False
    reported_volume = sum(float(deal.get("volume") or 0.0) for deal in entries)
    if (trade.expected_market_volume > 0
            and reported_volume + 1e-9 < trade.expected_market_volume):
        return False
    actual = sum(
        max((float(deal["price"]) - trade.stop) * trade.direction, 0.0)
        * broker.spec.value_per_point * float(deal.get("volume") or 0.0)
        for deal in entries
    )
    if actual <= 0:
        return False
    previous = trade.risk_cash
    trade.risk_cash = round(actual, 2)
    trade.converted_risk_pending = False
    journal.write(
        JOURNAL_PATH, "risk_cash_actualized", plan_id=trade.plan_id,
        previous_risk_cash=previous, risk_cash=trade.risk_cash,
        position_tickets=trade.position_tickets,
    )
    print(f"[RISK_ACTUALIZED] plan={trade.plan_id} "
          f"risk_cash={previous:.2f}->{trade.risk_cash:.2f}")
    return True


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


def _target_ticket_field(target: int, kind: str) -> str:
    if target not in (1, 2, 3):
        raise ValueError(f"invalid split target: {target}")
    if kind not in {"market_order", "position", "pending"}:
        raise ValueError(f"invalid split ticket kind: {kind}")
    return f"tp{target}_{kind}_ticket"


def _set_target_ticket(trade: ManagedTrade, target: int, kind: str,
                       ticket: int | None) -> None:
    """Persist the broker identity for one target leg."""
    setattr(trade, _target_ticket_field(target, kind), ticket)


def _hydrate_target_positions(trade: ManagedTrade,
                              open_positions: dict[int, Position]) -> bool:
    """Fill target -> position mappings from the broker's live TP prices.

    New state records map every leg explicitly. Older records only carried a
    TP1 identity, so use the authoritative live take-profit price first and
    retain the old sorted-list contract as a final migration fallback.
    """
    changed = False
    candidates: dict[int, list[int]] = {1: [], 2: [], 3: []}
    for ticket in trade.position_tickets:
        position = open_positions.get(ticket)
        if position is None:
            continue
        target = _position_target_index(trade, position) + 1
        if target in candidates:
            candidates[target].append(ticket)
    for target, tickets in candidates.items():
        # A unique broker TP price is authoritative. Duplicate/placeholder
        # prices are ambiguous (and common in test adapters), so leave them to
        # the legacy sorted-ticket fallback below.
        if len(tickets) != 1:
            continue
        field = _target_ticket_field(target, "position")
        if getattr(trade, field) is None:
            setattr(trade, field, tickets[0])
            changed = True

    # Before explicit target identities were added, market positions were
    # sorted by TP1/TP2/TP3 at creation and recovery. Only use that migration
    # path when all three tickets exist; guessing in a partial placement could
    # falsely treat TP2 as banked and move TP3's stop.
    if (len(trade.position_tickets) >= 3
            and all(getattr(trade, _target_ticket_field(target, "position")) is None
                    for target in (1, 2, 3))):
        for target, ticket in zip((1, 2, 3), trade.position_tickets[:3]):
            _set_target_ticket(trade, target, "position", ticket)
            changed = True
    return changed


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
    # Expected slippage is appropriate for expectancy, but a stop intended to
    # protect capital needs a tail reserve. On 2026-08-05 XAU BE stops at
    # 4183.98/4183.84 filled at 4183.55: $0.43/$0.29 adverse execution. Use the
    # larger protective value when configured; no finite reserve can protect a
    # true market gap, but this covers the observed ordinary stop slippage.
    slippage_points = max(
        float(costs.get("slippage_points", 0.0)),
        float(costs.get(
            "breakeven_slippage_points", costs.get("slippage_points", 0.0),
        )),
    )
    slippage_price = slippage_points * source_point
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


def _cost_covered_stop(broker: Broker, position: Position,
                       anchor: float) -> float:
    """Round a cost-covered stop away from the losing side."""
    raw = anchor + position.direction * _breakeven_cost_buffer(broker, position)
    scale = 10 ** broker.spec.digits
    # Round away from the losing side. Built-in round() can round a BUY stop
    # down (or a SELL stop up), silently discarding the last cost-covering tick.
    if position.direction == 1:
        return math.ceil(raw * scale - 1e-9) / scale
    return math.floor(raw * scale + 1e-9) / scale


def breakeven_stop(broker: Broker, position: Position) -> float:
    """Cost-covered break-even based on this leg's authoritative broker fill."""
    return _cost_covered_stop(broker, position, position.price_open)


def stepped_profit_stop(broker: Broker, trade: ManagedTrade,
                        position: Position) -> float:
    """Cost-covered TP1 lock for the surviving TP3 leg after TP2."""
    if not trade.targets:
        return breakeven_stop(broker, position)
    return _cost_covered_stop(broker, position, trade.targets[0])


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


def recover_orphan_setups(broker: Broker, state: BotState) -> list[ManagedTrade]:
    """Adopt unmistakable live positions/pending orders missing from state.

    A reset or stale state file must not leave live bot positions without TP1
    break-even and timeout management. Position recovery requires all three
    TP roles, a common plan stop, fills close in time, and entry dispersion no
    larger than the configured market slippage envelope. Pending recovery can
    safely retain a partial placement because every ticket still has its own
    broker-side SL/TP/expiry; an absent TP1 is explicitly marked unknown so it
    can never trigger a guessed break-even move.
    """
    positions = broker.positions()
    managed = {
        ticket for trade in state.open_trades()
        for ticket in trade.position_tickets
    }
    candidates = []
    for position in positions:
        if position.ticket in managed:
            continue
        match = _RECOVERABLE_COMMENT.search(position.comment or "")
        if match is None or position.stop <= 0 or position.take_profit <= 0:
            continue
        candidates.append((position, match.group("timeframe").upper(),
                           int(match.group("target"))))

    recovered = []
    live_by_ticket = {position.ticket: position for position in positions}
    unattached = []
    for position, timeframe, target in candidates:
        matches = []
        for trade in state.open_trades():
            if (trade.timeframe.upper() != timeframe
                    or trade.direction != position.direction
                    or trade.exit_mode != "be_33_33_34"
                    or len(trade.targets) < target or trade.risk <= 0):
                continue
            price_tolerance = max(broker.spec.point * 2, trade.risk * 0.02)
            if (abs(position.stop - trade.stop) > price_tolerance
                    or abs(position.take_profit - trade.targets[target - 1])
                    > price_tolerance
                    or abs(position.price_open - trade.entry) > trade.risk * 0.20):
                continue
            role_already_live = any(
                ticket in live_by_ticket
                and abs(live_by_ticket[ticket].take_profit
                        - trade.targets[target - 1]) <= price_tolerance
                for ticket in trade.position_tickets
            )
            if not role_already_live:
                matches.append(trade)
        if len(matches) != 1:
            unattached.append((position, timeframe, target))
            continue
        trade = matches[0]
        trade.position_tickets.append(position.ticket)
        _set_target_ticket(trade, target, "position", position.ticket)
        trade.filled_at = trade.filled_at or str(position.opened_at)
        trade.fill_bar_time = trade.fill_bar_time or str(position.opened_at)
        _count_trading_moment(state, position.opened_at)
        managed.add(position.ticket)
        if trade not in recovered:
            recovered.append(trade)
        journal.write(
            JOURNAL_PATH, "orphan_position_attached", plan_id=trade.plan_id,
            ticket=position.ticket, role=target,
        )
        print(f"[ORPHAN_ATTACHED] plan={trade.plan_id} "
              f"position={position.ticket} role=TP{target}")
    candidates = unattached

    groups: dict[tuple, list[tuple[Position, int]]] = {}
    for position, timeframe, target in candidates:
        key = (
            timeframe, position.direction,
            round(position.stop, broker.spec.digits),
        )
        groups.setdefault(key, []).append((position, target))

    for key, legs in groups.items():
        roles = [target for _, target in legs]
        if len(legs) != 3 or sorted(roles) != [1, 2, 3]:
            continue
        timeframe, direction, stop = key
        ordered = sorted(legs, key=lambda item: item[1])
        opened_times = [position.opened_at for position, _ in ordered]
        recovery_window = max(
            10.0, float(getattr(broker, "write_spacing_seconds", 0.0)) * 4 + 5,
        )
        if (max(opened_times) - min(opened_times)).total_seconds() > recovery_window:
            continue
        entries = [position.price_open for position, _ in ordered]
        risks = [(entry - stop) * direction for entry in entries]
        if min(risks) <= 0 or max(entries) - min(entries) > min(risks) * 0.20:
            continue
        # TP1/TP2/TP3 must progress farther in the profitable direction. This
        # rejects three unrelated tickets that merely reuse the same stop.
        directed_targets = [position.take_profit * direction
                            for position, _ in ordered]
        if not directed_targets[0] < directed_targets[1] < directed_targets[2]:
            continue
        entry = sum(entries) / len(entries)
        opened_at = max(opened_times)
        risk = (entry - stop) * direction
        position_tickets = [position.ticket for position, _ in ordered]
        if any(ticket in managed for ticket in position_tickets):
            continue
        base_id = f"{timeframe}@recovered-{opened_at.isoformat(sep=' ')}"
        plan_id = base_id
        suffix = 2
        while plan_id in state.trades:
            plan_id = f"{base_id}-{suffix}"
            suffix += 1
        trade = ManagedTrade(
            plan_id=plan_id, timeframe=timeframe, direction=direction,
            entry=entry, stop=stop, risk=risk,
            risk_cash=round(_positions_risk_cash(
                broker, [position for position, _ in ordered], stop,
            ), 2),
            targets=[position.take_profit for position, _ in ordered],
            legs=[position.volume for position, _ in ordered],
            position_tickets=position_tickets,
            filled_at=str(opened_at), fill_bar_time=str(opened_at),
            exit_mode="be_33_33_34", dry_run=False,
        )
        for target, (position, _) in enumerate(ordered, start=1):
            _set_target_ticket(trade, target, "position", position.ticket)
        state.trades[plan_id] = trade
        state.remember_plan(plan_id)
        _count_trading_moment(state, opened_at)
        managed.update(position_tickets)
        recovered.append(trade)
        journal.write(
            JOURNAL_PATH, "orphan_setup_recovered", plan_id=plan_id,
            timeframe=timeframe, direction=direction,
            position_tickets=position_tickets, risk_cash=trade.risk_cash,
        )
        print(f"[ORPHAN_RECOVERED] plan={plan_id} "
              f"positions={position_tickets} risk_cash={trade.risk_cash:.2f}")

    managed_pending = {
        ticket for trade in state.open_trades()
        for ticket in trade.pending_tickets
    }
    pending_groups: dict[tuple, list[tuple[dict, int]]] = {}
    for order in broker.pending_orders():
        if order["ticket"] in managed_pending:
            continue
        match = _RECOVERABLE_COMMENT.search(str(order.get("comment", "")))
        type_name = str(order.get("type_name", "")).upper()
        direction = (1 if type_name.startswith("BUY") else
                     -1 if type_name.startswith("SELL") else 0)
        entry = float(order.get("price") or 0.0)
        stop = float(order.get("stop") or 0.0)
        target_price = float(order.get("take_profit") or 0.0)
        if (match is None or not direction or entry <= 0 or stop <= 0
                or target_price <= 0 or (entry - stop) * direction <= 0):
            continue
        timeframe = match.group("timeframe").upper()
        role = int(match.group("target"))
        matches = []
        for trade in state.open_trades():
            if (trade.timeframe.upper() != timeframe
                    or trade.direction != direction
                    or trade.exit_mode != "be_33_33_34"
                    or len(trade.targets) < role or trade.risk <= 0):
                continue
            price_tolerance = max(broker.spec.point * 2, trade.risk * 0.02)
            if (abs(entry - trade.entry) <= price_tolerance
                    and abs(stop - trade.stop) <= price_tolerance
                    and abs(target_price - trade.targets[role - 1])
                    <= price_tolerance):
                matches.append(trade)
        if len(matches) == 1:
            trade = matches[0]
            trade.pending_tickets.append(order["ticket"])
            _set_target_ticket(trade, role, "pending", order["ticket"])
            managed_pending.add(order["ticket"])
            if trade not in recovered:
                recovered.append(trade)
            journal.write(
                JOURNAL_PATH, "orphan_pending_attached", plan_id=trade.plan_id,
                ticket=order["ticket"], role=role,
            )
            print(f"[ORPHAN_PENDING_ATTACHED] plan={trade.plan_id} "
                  f"order={order['ticket']} role=TP{role}")
            continue
        key = (
            timeframe, direction,
            round(entry, broker.spec.digits), round(stop, broker.spec.digits),
            order.get("expires_at"),
        )
        pending_groups.setdefault(key, []).append(
            (order, role),
        )

    for key, legs in pending_groups.items():
        roles = [role for _, role in legs]
        if len(set(roles)) != len(roles) or not 1 <= len(legs) <= 3:
            continue
        timeframe, direction, entry, stop, expires_at = key
        risk = (entry - stop) * direction
        targets = [entry + direction * risk * reward for reward in (1.0, 1.5, 2.0)]
        volumes = [0.0, 0.0, 0.0]
        by_role = {}
        for order, role in legs:
            targets[role - 1] = float(order["take_profit"])
            volumes[role - 1] = float(order["volume"])
            by_role[role] = order
        pending_tickets = [by_role[role]["ticket"] for role in sorted(by_role)]
        if any(ticket in managed_pending for ticket in pending_tickets):
            continue
        stamp = (expires_at.isoformat(sep=" ") if expires_at is not None
                 else f"tickets-{min(pending_tickets)}")
        base_id = f"{timeframe}@recovered-pending-{stamp}"
        plan_id = base_id
        suffix = 2
        while plan_id in state.trades:
            plan_id = f"{base_id}-{suffix}"
            suffix += 1
        risk_cash = sum(
            abs(float(order["price"]) - float(order["stop"]))
            * broker.spec.value_per_point * float(order["volume"])
            for order, _ in legs
        )
        trade = ManagedTrade(
            plan_id=plan_id, timeframe=timeframe, direction=direction,
            entry=entry, stop=stop, risk=risk, risk_cash=round(risk_cash, 2),
            targets=targets, legs=volumes,
            pending_tickets=pending_tickets,
            exit_mode="be_33_33_34", dry_run=False,
        )
        for target, order in by_role.items():
            _set_target_ticket(trade, target, "pending", order["ticket"])
        if 1 not in by_role:
            # Preserve the existing explicit-unknown sentinel used by startup
            # reconciliation: a missing TP1 must never be guessed as banked.
            trade.tp1_pending_ticket = -1
        state.trades[plan_id] = trade
        state.remember_plan(plan_id)
        managed_pending.update(pending_tickets)
        recovered.append(trade)
        journal.write(
            JOURNAL_PATH, "orphan_pending_recovered", plan_id=plan_id,
            timeframe=timeframe, direction=direction,
            pending_tickets=pending_tickets, roles=sorted(by_role),
            risk_cash=trade.risk_cash,
        )
        print(f"[ORPHAN_PENDING_RECOVERED] plan={plan_id} "
              f"orders={pending_tickets} roles={sorted(by_role)} "
              f"risk_cash={trade.risk_cash:.2f}")
    if recovered:
        state.save(STATE_PATH)
    return recovered


def open_trade(broker: Broker, settings: Settings, state: BotState, intent: Intent,
               balance: float, risk_percent: float | None = None) -> ManagedTrade | None:
    """Size the plan and place its legs. Returns None when nothing was sent."""
    # Never let current balance/equity flip the capital tier. `initial_balance`
    # is durable state captured on the first run; using it here as well as in the
    # caller makes the invariant survive refactors and direct calls.
    risk_basis = ((state.initial_balance or balance)
                  if settings.exit_mode == "capital_tier" else balance)
    exit_mode = settings.resolved_exit_mode(risk_basis)
    leg_weights = settings.leg_weights_for(risk_basis)
    requested_risk = (settings.risk_percent if risk_percent is None
                      else float(risk_percent))
    try:
        sizing = size_plan(broker.spec, risk_basis, requested_risk,
                           sizing_stop_distance(broker, settings, intent), leg_weights,
                           rounding=("down" if intent.action == "market"
                                     else settings.lot_rounding),
                           max_overshoot=(0.0 if intent.action == "market"
                                          else settings.max_risk_overshoot))
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
                      requested_risk_percent=requested_risk,
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
        # A converted attempt has already released its limit before this record
        # replaces the pending one. Preserve that fact before sending leg one:
        # if the broker rejects the first leg (or the process dies here), a
        # restart on the same closed bar must not treat the empty record as a
        # fresh conversion and retry the plan.
        conversion_released=intent.converted,
        # Every market entry is initially sized at its worst permitted fill.
        # Replace that conservative denominator with authoritative fill risk as
        # soon as positions/deals are visible; the legacy field name is retained
        # for state-file compatibility.
        converted_risk_pending=intent.action == "market",
        dry_run=broker.dry_run, exit_mode=actual_exit_mode,
    )

    expiry = None
    if intent.action == "limit":
        remaining = max(signals.limit_life_bars() - intent.bars_since_signal, 1)
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
                worst_price = (intent.entry + intent.direction * intent.risk
                               * settings.max_entry_slippage_r)
                result = broker.market_entry(
                    intent.direction, volume, intent.stop, target, comment,
                    worst_price=worst_price,
                )
                # A performed TRADE_ACTION_DEAL may expose only `deal`, while an
                # accepted/placed request normally exposes `order`. Persist
                # whichever authoritative identity MT5 supplied so delayed
                # position visibility cannot turn a real fill into an orphan.
                recovery_ticket = int(result.get("order") or result.get("deal") or 0)
                if recovery_ticket:
                    trade.market_order_tickets.append(recovery_ticket)
                    # MqlTradeResult reports the executed volume. Prefer it so a
                    # legitimate partial fill cannot leave risk reconciliation
                    # waiting forever for volume the broker never opened.
                    executed_volume = float(result.get("volume") or volume)
                    trade.expected_market_volume = round(
                        trade.expected_market_volume + executed_volume, 8
                    )
                    if actual_exit_mode == "be_33_33_34":
                        _set_target_ticket(
                            trade, index, "market_order", recovery_ticket,
                        )
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
                    if actual_exit_mode == "be_33_33_34":
                        _set_target_ticket(trade, index, "pending", ticket)
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
            # A converted plan fills bars after its signal, and the 120-bar
            # timeout counts from the fill. Anchoring it to the signal would
            # retire the trade early by exactly the bars spent waiting.
            trade.fill_bar_time = intent.fill_bar_time or intent.signal_time
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
            if (len(opened) >= placed
                    and sum(position.volume for position in opened) + 1e-9
                    >= trade.expected_market_volume):
                actual_risk_cash = _positions_risk_cash(broker, opened, trade.stop)
                if actual_risk_cash > 0:
                    trade.risk_cash = round(actual_risk_cash, 2)
                    trade.converted_risk_pending = False
            if opened and actual_exit_mode == "be_33_33_34":
                for position in opened:
                    target = _position_target_index(trade, position) + 1
                    if target in (1, 2, 3):
                        _set_target_ticket(
                            trade, target, "position", position.ticket,
                        )
            # Every accepted market leg is visible, so no history recovery is
            # outstanding. If visibility lags, retain the order/deal ids for
            # startup.
            if len(opened) >= placed:
                trade.market_order_tickets.clear()
            if trade.position_tickets:
                state.count_trading_day()
        if (not trade.position_tickets and not trade.pending_tickets
                and not trade.market_order_tickets):
            # Nothing is live, so nothing needs managing. It stays in `trades` as
            # a closed record, which also stops the plan being retried. Accepted
            # market recovery ids are live uncertainty, not "nothing": MT5 can
            # make the transaction visible in history a moment before its
            # position is returned by positions_get().
            trade.closed = True
        state.save(STATE_PATH)

    journal.write(JOURNAL_PATH, "trade_opened", plan_id=trade.plan_id,
                  side=intent.side, action=intent.action, timeframe=intent.timeframe,
                  entry=intent.entry, stop=intent.stop, risk=intent.risk,
                  targets=targets, legs=list(sizing.legs), legs_placed=placed,
                  risk_cash=trade.risk_cash,
                  requested_risk_percent=requested_risk,
                  single_leg=sizing.single_leg, exit_mode=actual_exit_mode,
                  converted=intent.converted,
                  dry_run=broker.dry_run)
    if intent.converted and placed:
        event = ("converted_market_simulated" if broker.dry_run
                 else "converted_market_opened")
        journal.write(JOURNAL_PATH, event, plan_id=trade.plan_id,
                      timeframe=intent.timeframe, legs_placed=placed)
    if trade.position_tickets:
        print(f"[POSITION_OPENED] plan={intent.plan_id} side={intent.side} "
              f"positions={trade.position_tickets} volumes={list(sizing.legs)} "
              f"entry={intent.entry} sl={intent.stop} targets={targets} "
              f"risk={requested_risk:.2f}% risk_cash={trade.risk_cash:.2f}")
    elif trade.pending_tickets:
        expiry_text = expiry.strftime("%Y-%m-%d %H:%M:%S") if expiry else "GTC"
        print(f"[PENDING_CREATED] plan={intent.plan_id} side={intent.side} "
              f"orders={trade.pending_tickets} volumes={list(sizing.legs)} "
              f"entry={intent.entry} sl={intent.stop} targets={targets} "
              f"expires={expiry_text} risk={requested_risk:.2f}% "
              f"risk_cash={trade.risk_cash:.2f}")
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

    MT5 order/deal tickets and position tickets are different identifiers on
    many brokers. Deal history provides the authoritative mapping.
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
            for target in (1, 2, 3):
                if order_ticket == getattr(
                        trade, _target_ticket_field(target, "market_order")):
                    _set_target_ticket(
                        trade, target, "position", position_ticket,
                    )
        trade.market_order_tickets = [
            ticket for ticket in original_market if ticket not in filled_by_order
        ]
        unresolved_market = list(trade.market_order_tickets)
        if recovered_market:
            changed = True
            recovered_positions = [
                filled_by_order[ticket] for ticket in recovered_market
            ]
            fill_moment = _recovered_fill_moment(
                broker, recovered_positions, frames, trade.timeframe,
            )
            trade.filled_at = trade.filled_at or str(fill_moment)
            if not trade.fill_bar_time:
                trade.fill_bar_time = str(fill_moment)
            _count_trading_moment(state, fill_moment)
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

        if trade.converted_risk_pending and trade.position_tickets:
            since = pd.Timestamp(
                trade.filled_at or broker.tick()["server_time"]
            ).to_pydatetime()
            changed = (_actualize_converted_risk_from_deals(
                broker, trade, broker.closed_deals(since - timedelta(days=2))
            ) or changed)

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
            for target in (1, 2, 3):
                if order_ticket == getattr(
                        trade, _target_ticket_field(target, "pending")):
                    _set_target_ticket(
                        trade, target, "position", position_ticket,
                    )
        changed = changed or trade.pending_tickets != original

        if filled_positions:
            fill_moment = _recovered_fill_moment(
                broker, filled_positions, frames, trade.timeframe,
            )
            trade.filled_at = trade.filled_at or str(fill_moment)
            if not trade.fill_bar_time:
                trade.fill_bar_time = str(fill_moment)
            _count_trading_moment(state, fill_moment)
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

def apply_breakeven(broker: Broker, state: BotState) -> set[int]:
    """Apply the split exit ladder and return live position tickets.

    All legs start with one stop. Once TP1 is gone while later legs remain, TP1
    must have been taken rather than stopped out, so TP2 and TP3 move to
    cost-covered break-even. Once the TP2 leg is also gone while TP3 remains,
    TP2 must have been taken, so TP3 moves to a cost-covered TP1 lock (+1R).

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
        _hydrate_target_positions(trade, open_positions)
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
        if not (trade.breakeven_done and not moved):
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

        # The second step is intentionally based on exact TP2/TP3 identities.
        # A missing TP2 position without a recorded fill is not evidence that
        # TP2 was banked: it may have been rejected, canceled, or still waiting
        # for MT5 history. Legacy three-position records retain the original
        # TP1/TP2/TP3 list ordering as a safe migration fallback.
        tp2_ticket = trade.tp2_position_ticket
        tp3_ticket = trade.tp3_position_ticket
        if tp2_ticket is None and (
                trade.tp2_market_order_ticket is not None
                or trade.tp2_pending_ticket is not None):
            continue
        if tp3_ticket is None and (
                trade.tp3_market_order_ticket is not None
                or trade.tp3_pending_ticket is not None):
            continue
        if tp2_ticket is None or tp3_ticket is None:
            if len(trade.position_tickets) < 3:
                continue
            tp2_ticket = trade.position_tickets[1]
            tp3_ticket = trade.position_tickets[2]
        if tp2_ticket in open_positions:
            continue
        tp3_position = open_positions.get(tp3_ticket)
        if tp3_position is None:
            continue

        desired_stop = stepped_profit_stop(broker, trade, tp3_position)
        improves = (
            not tp3_position.stop
            or (desired_stop > tp3_position.stop if trade.direction == 1
                else desired_stop < tp3_position.stop)
        )
        moved = False
        if improves:
            try:
                broker.move_stop(tp3_position, desired_stop)
            except OrderRejected as error:
                journal.write(
                    JOURNAL_PATH, "step_stop_rejected", plan_id=trade.plan_id,
                    trigger="TP2", ticket=tp3_position.ticket,
                    reason=str(error),
                )
                print(f"[STOP_REJECTED] ticket={tp3_position.ticket} "
                      f"reason={str(error)!r} scope='tp2_step'")
                continue
            moved = True

        # A prior process may have completed the broker write before crashing.
        # Treat an already tighter stop as complete, while still allowing a
        # later negative-swap update to tighten it further.
        if trade.tp2_lock_done and not moved:
            continue
        trade.tp2_lock_done = True
        buffer = round(
            _breakeven_cost_buffer(broker, tp3_position), broker.spec.digits,
        )
        journal.write(
            JOURNAL_PATH, "step_stop", plan_id=trade.plan_id,
            trigger="TP2", ticket=tp3_position.ticket,
            anchor=trade.targets[0] if trade.targets else None,
            stop=desired_stop, moved=moved, cost_buffer=buffer,
            swap=float(getattr(tp3_position, "swap", 0.0)),
        )
        if moved:
            print(f"[STOP_MOVED] plan={trade.plan_id} "
                  f"mode=TP2_STEP_TO_TP1 stop={desired_stop} "
                  f"position={tp3_position.ticket}")
        else:
            print(f"[STOP_CONFIRMED] plan={trade.plan_id} "
                  f"mode=TP2_STEP_TO_TP1 stop={desired_stop} "
                  f"position={tp3_position.ticket}")
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


def release_for_conversion(broker: Broker, state: BotState, intent: Intent) -> bool:
    """Clear the working limit so the plan can be re-sent at market.

    Returns True only when it is *provably* safe to send: every working order for
    this plan is gone and the plan holds no position. Anything else returns False
    and the caller must send nothing.

    The failure this exists to prevent is double size. A cancel and a fill can
    cross on the wire — the retrace prints on the very bar the bot decides to give
    up on it — and a market order sent on the assumption the cancel worked would
    leave two positions on one plan, each sized for the whole risk budget. So the
    broker is asked twice: once to cancel, and once afterwards to confirm that
    nothing of this plan is live. A missed conversion costs one trade; an
    unnoticed double fill costs twice the risk the account is allowed to carry.
    """
    trade = state.trades.get(intent.plan_id)
    if trade is None:
        # Usually this means the bot was offline while the limit would have been
        # working, so there is genuinely nothing to release.  A lost/reset state
        # file is indistinguishable locally, though, and may leave an orphan from
        # this timeframe at MT5. Order comments start with the timeframe; refuse
        # that ambiguous case and let the operator/startup reconciliation surface
        # it instead of stacking a fresh full-risk market setup on top.
        same_timeframe_orders = [
            order["ticket"] for order in broker.pending_orders()
            if str(order.get("comment", "")).upper().startswith(
                f"{intent.timeframe.upper()} ")
        ]
        same_timeframe_positions = [
            position.ticket for position in broker.positions()
            if str(getattr(position, "comment", "")).upper().startswith(
                f"{intent.timeframe.upper()} ")
        ]
        if same_timeframe_orders or same_timeframe_positions:
            journal.write(
                JOURNAL_PATH, "convert_aborted", plan_id=intent.plan_id,
                reason="same-timeframe broker exposure has no state record",
                orders=same_timeframe_orders, positions=same_timeframe_positions,
            )
            print(f"[CONVERT_ABORTED] plan={intent.plan_id} "
                  f"orders={same_timeframe_orders} positions={same_timeframe_positions} "
                  "reason='untracked same-timeframe exposure'")
            return False
        return True

    if trade.conversion_released:
        journal.write(JOURNAL_PATH, "convert_skipped", plan_id=intent.plan_id,
                      reason="limit was already released for conversion")
        print(f"[CONVERT_SKIPPED] plan={intent.plan_id} reason='already released'")
        return False

    if trade.position_tickets or trade.market_order_tickets:
        journal.write(JOURNAL_PATH, "convert_skipped", plan_id=intent.plan_id,
                      reason="plan already holds a position")
        print(f"[CONVERT_SKIPPED] plan={intent.plan_id} reason='already filled'")
        return False

    # Snapshot before touching anything. A filled pending order does not keep its
    # order ticket — MT5 issues a position ticket and only the deal history ties
    # the two together — so "did one of my orders fill?" cannot be answered by
    # comparing ticket sets. What can be answered, without any mapping, is
    # "did a position appear that was not there a moment ago?"
    before = {position.ticket for position in broker.positions()}
    working = {order["ticket"] for order in broker.pending_orders()}
    tracked = set(trade.pending_tickets)
    disappeared = tracked - working
    if disappeared:
        # This is the race window that a post-cancel position diff cannot see:
        # the order may have filled after the pass's sync, but before `before`
        # was captured. Its position is then already part of `before`, so it
        # would not "appear" below. Missing is therefore uncertainty, never
        # proof of cancellation; leave the ticket owned for `sync_fills` to map
        # through deal history on the next pass.
        disappeared_states = broker.finished_order_states(sorted(disappeared))
        safe_states = {"CANCELED", "EXPIRED", "REJECTED"}
        unsafe_disappeared = {
            ticket: disappeared_states.get(ticket) for ticket in disappeared
            if disappeared_states.get(ticket) not in safe_states
        }
        if unsafe_disappeared:
            journal.write(JOURNAL_PATH, "convert_aborted", plan_id=intent.plan_id,
                          reason="pending order disappeared before cancel",
                          states=unsafe_disappeared)
            print(f"[CONVERT_ABORTED] plan={intent.plan_id} "
                  f"states={unsafe_disappeared} "
                  "reason='pending disappeared before cancel; awaiting history'")
            return False

    for ticket in list(trade.pending_tickets):
        if ticket not in working:
            continue
        try:
            broker.cancel(ticket, "converting to market")
        except OrderRejected as error:
            journal.write(JOURNAL_PATH, "convert_cancel_rejected",
                          plan_id=intent.plan_id, ticket=ticket, reason=str(error))
            print(f"[CONVERT_ABORTED] plan={intent.plan_id} ticket={ticket} "
                  f"reason={str(error)!r}")
            return False

    # Re-read rather than trust the cancels. `order_send` returning done is not
    # proof the order was cancelled rather than filled a millisecond earlier.
    appeared = {position.ticket for position in broker.positions()} - before
    if appeared:
        journal.write(JOURNAL_PATH, "convert_aborted", plan_id=intent.plan_id,
                      reason="position appeared while cancelling",
                      tickets=sorted(appeared))
        print(f"[CONVERT_ABORTED] plan={intent.plan_id} positions={sorted(appeared)} "
              "reason='filled while cancelling'")
        return False
    still_working = {order["ticket"] for order in broker.pending_orders()}
    if still_working & set(trade.pending_tickets):
        journal.write(JOURNAL_PATH, "convert_aborted", plan_id=intent.plan_id,
                      reason="order still working after cancel")
        print(f"[CONVERT_ABORTED] plan={intent.plan_id} reason='order still working'")
        return False

    # Position visibility can lag behind the order leaving the live book.  Do
    # not infer "cancelled" from those two observations; order history is the
    # authoritative terminal state.  A history response that is incomplete is
    # also not proof, so prefer missing one conversion to risking a double fill.
    finished = broker.finished_order_states(sorted(tracked))
    safe_states = {"CANCELED", "EXPIRED", "REJECTED"}
    unsafe = {ticket: finished.get(ticket) for ticket in tracked
              if finished.get(ticket) not in safe_states}
    if unsafe:
        journal.write(JOURNAL_PATH, "convert_aborted", plan_id=intent.plan_id,
                      reason="cancel not confirmed by order history", states=unsafe)
        print(f"[CONVERT_ABORTED] plan={intent.plan_id} states={unsafe} "
              "reason='cancel not confirmed by history'")
        return False

    trade.pending_tickets.clear()
    # The old limit record has no live exposure now. Marking it closed prevents
    # a blocked market replacement from becoming an unprunable zombie; the
    # successful market path immediately replaces this record in `open_trade`.
    trade.closed = True
    trade.conversion_released = True
    state.save(STATE_PATH)
    journal.write(JOURNAL_PATH, "limit_released_for_conversion",
                  plan_id=intent.plan_id, timeframe=intent.timeframe)
    print(f"[CONVERT] plan={intent.plan_id} reason='retrace never came' "
          f"scope='limit cancelled, sending market'")
    return True


def reconcile_closed(broker: Broker, state: BotState) -> list[dict]:
    """Score finished trades in R and update the loss streak."""
    open_tickets = {position.ticket for position in broker.positions()}
    pending_tickets = {order["ticket"] for order in broker.pending_orders()}
    finished = []
    current_day_outcomes = []
    for sequence, trade in enumerate(state.open_trades()):
        if trade.dry_run or not trade.position_tickets:
            continue
        # A partial history response can map TP1 before the survivor market
        # orders. If TP1 is already gone, its closing deal is enough to make the
        # mapped subset look finished, but it is not enough to score and close
        # the whole setup while survivor recovery ids are still unresolved.
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
        position_deals = [
            deal for deal in broker.closed_deals(since - timedelta(days=2))
            if deal["position"] in trade.position_tickets
        ]
        risk_actualized = _actualize_converted_risk_from_deals(
            broker, trade, position_deals
        )
        if risk_actualized:
            state.save(STATE_PATH)
        if trade.converted_risk_pending:
            print(f"[RECONCILE_WAIT] plan={trade.plan_id} "
                  "reason='converted entry deal history is incomplete'")
            continue
        deals = [
            deal for deal in position_deals
            # Legacy test/fake brokers predate the classification and only
            # return closing deals here, so absence retains that contract.
            if deal.get("is_exit", True)
        ]
        # `net` is profit plus commission plus swap. Gold on M30 can be held for
        # 60 hours, so swap is not a rounding error, and R is the number that
        # decides whether this edge survived contact with a real broker.
        if not deals:
            print(f"[RECONCILE_WAIT] plan={trade.plan_id} "
                  "reason='position disappeared but exit deal history is not available yet'")
            continue
        exits_by_position = {
            ticket: [deal for deal in deals if deal["position"] == ticket]
            for ticket in trade.position_tickets
        }
        missing_exits = [ticket for ticket, exits in exits_by_position.items()
                         if not exits]
        if missing_exits:
            print(f"[RECONCILE_WAIT] plan={trade.plan_id} "
                  f"positions={missing_exits} "
                  "reason='exit deal history is incomplete'")
            continue
        # A single position can close through multiple partial deals. When its
        # opening deal is present, use that authoritative broker volume (rather
        # than the planned leg list, which can differ after partial placement)
        # and require the full exit before scoring.
        incomplete_volumes = {}
        for ticket in trade.position_tickets:
            entry_rows = [deal for deal in position_deals
                          if deal["position"] == ticket
                          and not deal.get("is_exit", True)]
            exit_rows = exits_by_position[ticket]
            entry_volumes = [deal.get("volume") for deal in entry_rows]
            exit_volumes = [deal.get("volume") for deal in exit_rows]
            if (entry_volumes and exit_volumes
                    and all(volume is not None for volume in entry_volumes)
                    and all(volume is not None for volume in exit_volumes)):
                expected = sum(float(volume) for volume in entry_volumes)
                reported = sum(float(volume) for volume in exit_volumes)
                if reported + 1e-9 < expected:
                    incomplete_volumes[ticket] = round(reported, 8)
        if incomplete_volumes:
            print(f"[RECONCILE_WAIT] plan={trade.plan_id} "
                  f"volumes={incomplete_volumes} "
                  "reason='exit deal volume is incomplete'")
            continue
        # Once every exit is complete, score the entire position history. Entry
        # deals carry opening commission/fees even though they carry no closing
        # P/L; dropping them would flatter every live R.
        profit = sum(deal["net"] for deal in position_deals)
        costs = sum(
            deal["commission"] + deal["swap"] + deal.get("fee", 0.0)
            for deal in position_deals
        )
        r_value = profit / trade.risk_cash if trade.risk_cash else None
        trade.closed = True
        # A process can be offline for several FTMO days. Deal history then
        # contains old closures, and applying those outcomes after today's
        # roll_day() would manufacture today's realised loss/loss streak from
        # yesterday's trading. Production deals all carry broker-server time;
        # legacy fake adapters without it retain the old current-day contract.
        timed_deals = [deal for deal in position_deals
                       if isinstance(deal.get("time"), datetime)]
        timed_exits = [deal for deal in deals
                       if isinstance(deal.get("time"), datetime)]
        if timed_deals and state.server_utc_offset is not None:
            today_profit = sum(
                deal["net"] for deal in timed_deals
                if ftmo_day(deal["time"], state.server_utc_offset).isoformat()
                == state.day_key
            )
            state.day_realised += today_profit
            latest_exit = max(deal["time"] for deal in timed_exits)
            closes_today = (
                ftmo_day(latest_exit, state.server_utc_offset).isoformat()
                == state.day_key
            )
        else:
            state.day_realised += profit
            latest_exit = None
            closes_today = True
        if closes_today:
            current_day_outcomes.append((latest_exit, sequence, profit))
        record = journal.write(JOURNAL_PATH, "trade_closed", plan_id=trade.plan_id,
                               timeframe=trade.timeframe, profit=round(profit, 2),
                               costs=round(costs, 2),
                               r=round(r_value, 4) if r_value is not None else None,
                               breakeven_done=trade.breakeven_done)
        finished.append(record)
        scored = f" ({r_value:+.2f}R)" if r_value is not None else ""
        print(f"[POSITION_CLOSED] plan={trade.plan_id} net={profit:+.2f} "
              f"result={scored.strip() or 'n/a'} costs={costs:+.2f} "
              f"closure_day={'current' if closes_today else 'historical'}")
    # State dictionary order is creation order, not necessarily close order.
    # Several trades can finish while the process is offline, so replay today's
    # outcomes chronologically before deciding whether the loss streak pauses
    # new entries.
    current_day_outcomes.sort(key=lambda row: (
        row[0] is None, row[0] or datetime.min, row[1],
    ))
    for _, _, profit in current_day_outcomes:
        if profit < 0:
            state.consecutive_losses += 1
        elif profit > 0:
            state.consecutive_losses = 0
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
