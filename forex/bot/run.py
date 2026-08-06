"""Bot entry point.

    python -m bot.run --status          # account, guards and live R stats
    python -m bot.run --once            # one pass, dry-run
    python -m bot.run --reconcile      # startup/state reconciliation only
    python -m bot.run                   # loop, dry-run (safe default)
    python -m bot.run --live            # loop, sends real orders
    python -m bot.run --flatten --live  # emergency: cancel and close everything

Dry-run is the default everywhere. `--live` is the only way orders reach the
broker, and it is deliberately not settable from settings.local.json.

The loop only acts when a bar closes, because that is when the strategy was
measured. Between bar closes it still manages open trades: break-even moves,
timeouts and closed-trade accounting all keep running.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import timedelta

from strategy import config
from strategy.mt5_source import MT5Error

from engine import journal, market_hours, news, signals
from . import guardrails, settings as settings_module, trader
from .broker import Broker, OrderRejected
from engine.instance_lock import LiveInstanceLock
from .settings import BOT_DIR, JOURNAL_PATH, KILL_SWITCH, STATE_PATH
from engine.sizing import open_risk_percent, pending_risk_percent
from engine import dynamic_risk
from engine.state import BotState, ftmo_day, ftmo_day_start_server

LIVE_LOCK_PATH = BOT_DIR / ".live.lock"
STALE_FEED_RECHECK_SECONDS = 300.0


class _Ansi:
    RESET = "\x1b[0m"
    BOLD = "\x1b[1m"
    DIM = "\x1b[2m"
    RED = "\x1b[91m"
    GREEN = "\x1b[92m"
    YELLOW = "\x1b[93m"
    BLUE = "\x1b[94m"
    MAGENTA = "\x1b[95m"
    CYAN = "\x1b[96m"
    WHITE = "\x1b[97m"


def _color_enabled() -> bool:
    """Use colour only on an interactive terminal; redirected logs stay clean."""
    if os.environ.get("NO_COLOR") is not None or os.environ.get("TERM") == "dumb":
        return False
    if not getattr(sys.stdout, "isatty", lambda: False)():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


def paint(text: str, *styles: str) -> str:
    """Apply ANSI styling without changing the printable width."""
    if not styles or not _color_enabled():
        return text
    return "".join(styles) + text + _Ansi.RESET


def status_line(tag: str, message: str, level: str = "info") -> str:
    colours = {
        "ok": _Ansi.GREEN,
        "warn": _Ansi.YELLOW,
        "error": _Ansi.RED,
        "live": _Ansi.MAGENTA,
        "info": _Ansi.CYAN,
    }
    return f"{paint(f'[{tag}]', _Ansi.BOLD, colours.get(level, _Ansi.CYAN))} {message}"


def resolve_offset(broker: Broker, state: BotState, config_, *,
                   quote: dict | None = None,
                   measure: bool = True) -> tuple[float, str]:
    """Server-to-UTC offset, optionally reusing the current tick.

    The offset is stable within a trading day. Signal passes therefore use the
    last measured value instead of spending two tick requests every 15 minutes;
    startup/status/reconnect still measure it from a live quote.
    """
    if not measure:
        if state.server_utc_offset is not None:
            return state.server_utc_offset, "remembered"
        return config_.fallback_server_utc_offset, "fallback"
    measured = broker.server_utc_offset(quote=quote)
    if measured is not None:
        if measured != state.server_utc_offset:
            print(f"[CLOCK] broker server runs UTC{measured:+g}")
        state.server_utc_offset = measured
        return measured, "measured"
    if state.server_utc_offset is not None:
        return state.server_utc_offset, "remembered"
    return config_.fallback_server_utc_offset, "fallback"


EXIT_LABELS = {
    "fixed_tp3": "one leg to TP3 (2R)",
    "be_33_33_34": "three legs 33/33/34, BE after TP1, step after TP2",
    "capital_tier": "fixed TP3 below threshold; 33/33/34 + BE/step at/above threshold",
    "auto": "split when the size allows (NOT RECOMMENDED)",
}
#: `exit_mode` value that corresponds to each technique the lab can select.
TECHNIQUE_TO_MODE = {"fixed_tp3": "fixed_tp3",
                     "be_after_tp1_33_33_34": "be_33_33_34"}


def exit_mode_line(config_, initial_balance: float | None = None) -> str:
    """Show production exit and distinguish intentional tier overrides.

    A capital-tier policy is a deliberate production contract: it may use the
    robust split exit even when a timeframe's validation ranking picks full TP3.
    That is research context, not a runtime configuration error.
    """
    active_mode = config_.resolved_exit_mode(initial_balance)
    active_label = EXIT_LABELS.get(active_mode, active_mode)
    if config_.exit_mode == "capital_tier":
        label = (f"capital_tier ${config_.split_exit_min_balance:,.0f} -> "
                 f"{active_mode} ({active_label})")
    else:
        label = f"{active_mode} — {active_label}"
    try:
        from strategy import backtest_reporting as br

        mismatched = []
        research = []
        for timeframe in config_.timeframes:
            path = br.report_path(config_.symbol, timeframe)
            if not path.exists():
                continue
            picked = br.select_technique(br.load_report(path))
            if config_.exit_mode == "capital_tier":
                research.append(f"{timeframe}={picked}")
            elif TECHNIQUE_TO_MODE.get(picked, picked) != active_mode:
                mismatched.append(f"{timeframe} wants {picked}")
        if mismatched:
            return f"{label}    <-- CHECK: {', '.join(mismatched)}"
        if research:
            return f"{label}    research baseline: {', '.join(research)}"
        return f"{label}    matches the study"
    except Exception as error:                    # noqa: BLE001 - reporting only
        return f"{label}    (could not check: {error})"


def sizing_line(broker: Broker, config_, balance: float,
                risk_percent: float | None = None) -> str:
    """What a typical trade will really be sized at, per traded timeframe.

    Broker lot steps can put actual risk above or below the requested percentage.
    The preview uses the same rounding cap and capital-tier weights as execution,
    so a split tier that cannot produce three legal legs is visible here.
    """
    from engine.sizing import SizingError, size_plan

    requested = config_.risk_percent if risk_percent is None else risk_percent
    parts = []
    for timeframe in config_.timeframes:
        stop = _typical_stop(broker, config_, timeframe)
        if stop is None:
            parts.append(f"{timeframe} ?")
            continue
        try:
            # Preview the conservative market path used by open_trade(). A
            # plain stop distance with nearest rounding could display >1% even
            # though execution now reserves slippage and always rounds down.
            market_stop = (
                stop * (1 + config_.max_entry_slippage_r)
                + config_.deviation_points * broker.spec.point
            )
            s = size_plan(broker.spec, balance, requested, market_stop,
                          config_.leg_weights_for(balance),
                          rounding="down", max_overshoot=0.0)
        except SizingError:
            parts.append(f"{timeframe} REFUSED (stop {stop:.2f})")
            continue
        if (config_.resolved_exit_mode(balance) == "be_33_33_34"
                and s.single_leg):
            parts.append(f"{timeframe} REFUSED (needs 3 legs; stop {stop:.2f})")
            continue
        actual_pct = s.risk_cash / balance * 100 if balance else 0.0
        lots = "+".join(f"{leg:g}" for leg in s.legs)
        flag = ("" if s.risk_shortfall >= 0.85
                else f" ({s.risk_shortfall:.0%} sized)")
        parts.append(f"{timeframe} {lots}lot={actual_pct:.2f}%{flag}")
    asked = f"asked {requested:.2f}%"
    return f"{asked}   " + "   ".join(parts)


def _typical_stop(broker: Broker, config_, timeframe: str) -> float | None:
    """Median stop distance over recent bars, for the sizing preview only."""
    from strategy import quantum

    try:
        frame = broker.bars(timeframe, 1500)
        plans = quantum.analyse(frame, timeframe)["plans"]
    except Exception:                                 # noqa: BLE001 - preview only
        return None
    risks = [p["risk"] for p in plans if p["entry_fill_index"] is not None]
    if not risks:
        return None
    risks.sort()
    return float(risks[len(risks) // 2])


def _proposed_risk_percent(broker: Broker, config_, intent, balance: float,
                           risk_percent: float | None = None) -> float | None:
    """Risk this plan will really carry, as a percent of the risk basis.

    None when the plan cannot be sized at all — the caller should let the sizing
    step produce the proper refusal rather than have the exposure cap guess.
    """
    from engine.sizing import SizingError, size_plan

    if balance <= 0:
        return None
    try:
        requested = config_.risk_percent if risk_percent is None else risk_percent
        sizing = size_plan(broker.spec, balance, requested,
                           trader.sizing_stop_distance(broker, config_, intent),
                           config_.leg_weights_for(balance),
                           rounding=("down" if intent.action == "market"
                                     else config_.lot_rounding),
                           max_overshoot=(0.0 if intent.action == "market"
                                          else config_.max_risk_overshoot))
    except SizingError:
        return None
    return sizing.risk_cash / balance * 100


def _largest_leg(broker: Broker, config_, intent, balance: float,
                 risk_percent: float | None = None) -> float:
    """Volume of the biggest leg, for the margin question. 0 if unsizeable."""
    from engine.sizing import SizingError, size_plan

    try:
        requested = config_.risk_percent if risk_percent is None else risk_percent
        return max(size_plan(broker.spec, balance, requested,
                             trader.sizing_stop_distance(broker, config_, intent),
                             config_.leg_weights_for(balance),
                             rounding=("down" if intent.action == "market"
                                       else config_.lot_rounding),
                             max_overshoot=(0.0 if intent.action == "market"
                                            else config_.max_risk_overshoot)).legs)
    except SizingError:
        return 0.0


def _fit_dynamic_setup_risk(broker: Broker, config_, state: BotState, intent,
                            risk_basis: float, equity: float, balance: float,
                            positions, orders, live_risk: float,
                            requested_risk: float) -> tuple[float, float | None]:
    """Largest configured tier whose actual rounded risk fits every risk cap."""
    original = _proposed_risk_percent(
        broker, config_, intent, risk_basis, requested_risk)
    if not (config_.dynamic_risk_enabled
            and config_.dynamic_risk_fit_remaining):
        return requested_risk, original

    for candidate in dynamic_risk.fitting_tiers(config_, requested_risk):
        proposed = _proposed_risk_percent(
            broker, config_, intent, risk_basis, candidate)
        if proposed is None:
            continue
        total_fits = live_risk + proposed <= (
            config_.max_open_risk_percent + 1e-9)
        daily_fits = guardrails.projected_internal_daily_risk(
            config_, state, equity, risk_basis, live_risk, proposed,
            balance=balance)
        max_loss_fits = guardrails.projected_max_loss_risk(
            config_, state, risk_basis, live_risk, proposed, balance)
        idea_fits = guardrails.risk_per_idea(
            config_, broker.spec, positions, intent.direction, risk_basis,
            proposed_risk=proposed, orders=orders)
        if total_fits and daily_fits and max_loss_fits and idea_fits:
            return candidate, proposed
    return requested_risk, original


def _bar_hours(config_) -> float:
    """Length of the longest traded bar, in hours."""
    return max(config.TIMEFRAME_SECONDS[timeframe.upper()]
               for timeframe in config_.timeframes) / 3600


def frames_for_status(broker: Broker, config_, bars: int = 2000):
    """Recent bars on the longest timeframe, for measuring the real close."""
    longest = max(config_.timeframes,
                  key=lambda timeframe: config.TIMEFRAME_SECONDS[timeframe.upper()])
    try:
        return broker.bars(longest, bars)
    except MT5Error:
        return None


def describe_order(order: dict) -> str:
    expiry = order.get("expires_at")
    expiry_text = expiry.strftime("%Y-%m-%d %H:%M:%S") if expiry else "GTC"
    return (f"#{order['ticket']} {order.get('type_name', order.get('type', 'UNKNOWN'))} "
            f"{order.get('volume', 0):g} @ {order.get('price', 0)} "
            f"sl {order.get('stop', 0)} tp {order.get('take_profit', 0)} "
            f"expires {expiry_text} comment={order.get('comment', '')!r}")


def floating_pnl(positions) -> float:
    """Open P/L including cumulative swap reported separately by MT5."""
    return sum(
        position.profit + float(getattr(position, "swap", 0.0))
        for position in positions
    )


def checkpoint_state(broker: Broker, state: BotState) -> None:
    """Persist terminal request usage together with the latest durable state."""
    state.day_requests += broker.take_requests()
    state.save(STATE_PATH)


def print_exposure(positions, orders, *, label: str = "EXPOSURE") -> None:
    floating = floating_pnl(positions)
    print(f"[{label}] open_positions={len(positions)} pending_orders={len(orders)} "
          f"floating_pnl={floating:+.2f}")
    if not positions:
        print("[POSITION] none")
    for position in positions:
        print(f"[POSITION] {trader.describe(position)}")
    if not orders:
        print("[PENDING] none")
    for order in orders:
        print(f"[PENDING] {describe_order(order)}")

def print_management_alerts(state: BotState, positions, orders) -> None:
    managed_positions = {
        ticket for trade in state.open_trades() for ticket in trade.position_tickets
    }
    managed_orders = {
        ticket for trade in state.open_trades() for ticket in trade.pending_tickets
    }
    for position in positions:
        if position.ticket not in managed_positions:
            print(f"[ALERT] UNTRACKED_POSITION | {trader.describe(position)}")
    for order in orders:
        if order["ticket"] not in managed_orders:
            print(f"[ALERT] UNTRACKED_PENDING | {describe_order(order)}")


def active_setup_count(state: BotState, positions, orders) -> int:
    """Managed trade ideas, not broker tickets.

    A three-target exit is one setup represented by three MT5 positions. Counting
    tickets made a single split trade exceed a two-trade cap. Unknown tickets are
    still counted individually so an orphan can never create room for new risk.
    """
    live_position_tickets = {position.ticket for position in positions}
    live_order_tickets = {order["ticket"] for order in orders}
    managed_positions: set[int] = set()
    managed_orders: set[int] = set()
    managed_setups = 0
    for trade in state.open_trades():
        trade_positions = set(trade.position_tickets)
        trade_orders = set(trade.pending_tickets)
        unresolved_market_orders = set(trade.market_order_tickets)
        managed_positions.update(trade_positions)
        managed_orders.update(trade_orders)
        if (trade_positions & live_position_tickets
                or trade_orders & live_order_tickets
                or trade_positions or trade_orders
                or unresolved_market_orders):
            managed_setups += 1
    untracked = len(live_position_tickets - managed_positions)
    untracked += len(live_order_tickets - managed_orders)
    return managed_setups + untracked


def needs_split_management(state: BotState) -> bool:
    """Whether a split trade needs ticket sync, BE/step, or swap refresh."""
    return any(
        trade.exit_mode == "be_33_33_34"
        and (
            len(trade.position_tickets) >= 2
            or bool(trade.pending_tickets)
            or bool(trade.market_order_tickets)
        )
        for trade in state.open_trades()
    )


def _trade_for_ticket(state: BotState, ticket: int, *, pending: bool = False):
    field = "pending_tickets" if pending else "position_tickets"
    return next(
        (trade for trade in state.open_trades()
         if ticket in getattr(trade, field)),
        None,
    )


def _target_name(trade, target: float) -> str:
    if not trade or not trade.risk:
        return "TP?"
    reward_r = (target - trade.entry) * trade.direction / trade.risk
    choices = ((1.0, "TP1"), (1.5, "TP2"), (2.0, "TP3"))
    return min(choices, key=lambda item: abs(item[0] - reward_r))[1]


def position_health(position, state: BotState, quote: dict,
                    broker: Broker | None = None) -> str:
    """Explain what owns a position and how far price is from its exits."""
    trade = _trade_for_ticket(state, position.ticket)
    side = "BUY" if position.direction == 1 else "SELL"
    if trade is None:
        return (f"ticket={position.ticket} side={side} status=UNTRACKED "
                f"sl={position.stop} tp={position.take_profit}")

    exit_price = quote["bid"] if position.direction == 1 else quote["ask"]
    risk = trade.risk or abs(trade.entry - trade.stop)
    now_r = ((exit_price - trade.entry) * trade.direction / risk
             if risk else 0.0)
    to_sl_r = ((exit_price - position.stop) * trade.direction / risk
               if risk and position.stop else float("-inf"))
    to_tp_r = ((position.take_profit - exit_price) * trade.direction / risk
               if risk and position.take_profit else float("inf"))
    flags = []
    if not position.stop:
        flags.append("MISSING_SL")
    if not position.take_profit:
        flags.append("MISSING_TP")
    role = _target_name(trade, position.take_profit)
    if position.stop:
        # Broker price precision rounds the planned stop (4047.9359 -> 4047.94
        # on two-digit gold). Treat sub-basis-point differences as formatting,
        # not a widened-risk alert.
        price_tolerance = max(risk * 1e-3, 1e-9)
        widened = (position.stop < trade.stop - price_tolerance
                   if trade.direction == 1
                   else position.stop > trade.stop + price_tolerance)
        if widened:
            flags.append("SL_WIDER_THAN_PLAN")
        if trade.breakeven_done:
            fill_tolerance = max(risk * 1e-6, 1e-9)
            worse_than_fill = (
                position.stop < position.price_open - fill_tolerance
                if trade.direction == 1
                else position.stop > position.price_open + fill_tolerance
            )
            at_fill = abs(position.stop - position.price_open) <= fill_tolerance
            stepped = trade.tp2_lock_done and role == "TP3"
            desired_net_stop = (
                trader.stepped_profit_stop(broker, trade, position)
                if broker is not None and stepped
                else trader.breakeven_stop(broker, position)
                if broker is not None else position.price_open
            )
            below_net = (
                position.stop < desired_net_stop - fill_tolerance
                if trade.direction == 1
                else position.stop > desired_net_stop + fill_tolerance
            )
            if worse_than_fill:
                # A historical bug moved split survivors to the signal entry,
                # which can be materially worse than their market fill.
                flags.append("BE_STOP_BELOW_FILL")
            elif below_net:
                # Commission, slippage or newly accrued negative swap is not
                # fully covered yet. The fast management loop will tighten it.
                flags.append("BE_STOP_BELOW_NET")
            elif at_fill:
                # Gross price break-even still loses commission.
                flags.append("SL_AT_GROSS_BE")
            elif stepped:
                flags.append("SL_AT_TP1_STEP")
            else:
                flags.append("SL_AT_NET_BE")
    status = ",".join(flags) if flags else "PROTECTED"
    return (
        f"ticket={position.ticket} plan={trade.plan_id} tf={trade.timeframe} "
        f"mode={trade.exit_mode or 'legacy'} role={role} side={side} "
        f"price={exit_price:.2f} now={now_r:+.2f}R "
        f"to_sl={to_sl_r:+.2f}R to_tp={to_tp_r:+.2f}R "
        f"sl={position.stop} tp={position.take_profit} status={status}"
    )


def pending_health(order: dict, state: BotState, now) -> str:
    """Explain the setup and remaining lifetime of a pending order."""
    trade = _trade_for_ticket(state, order["ticket"], pending=True)
    expiry = order.get("expires_at")
    if expiry is None:
        expiry_text = "GTC"
    else:
        seconds = (expiry - now).total_seconds()
        expiry_text = ("EXPIRED" if seconds <= 0
                       else f"{_countdown(seconds)} remaining")
    if trade is None:
        return f"ticket={order['ticket']} status=UNTRACKED expiry={expiry_text}"
    return (
        f"ticket={order['ticket']} plan={trade.plan_id} tf={trade.timeframe} "
        f"mode={trade.exit_mode or 'legacy'} role={_target_name(trade, order.get('take_profit', 0))} "
        f"entry={order.get('price', 0)} sl={order.get('stop', 0)} "
        f"tp={order.get('take_profit', 0)} expiry={expiry_text} status=WORKING"
    )


def entry_capacity(config_, state: BotState, positions, orders, spec,
                   risk_basis: float, requests: int,
                   entry_gate=None,
                   setup_risk: float | None = None,
                   equity: float | None = None,
                   balance: float | None = None) -> tuple[bool, str]:
    """Capacity for another nominal-risk setup; signal checks still run later."""
    open_risk = (open_risk_percent(spec, positions, risk_basis)
                 + pending_risk_percent(spec, orders, risk_basis))
    requested = config_.risk_percent if setup_risk is None else setup_risk
    active = active_setup_count(state, positions, orders)
    blockers = []
    if entry_gate is not None and not entry_gate:
        blockers.append(entry_gate.reason)
    if state.halted_forever:
        blockers.append(f"halted: {state.halted_reason}")
    if state.is_paused_today:
        blockers.append("paused for this FTMO day")
    if state.consecutive_losses >= config_.max_consecutive_losses:
        blockers.append(f"loss streak {state.consecutive_losses}")
    if KILL_SWITCH.exists():
        blockers.append("kill switch active")
    if active >= config_.max_concurrent_trades:
        blockers.append(f"setup slots {active}/{config_.max_concurrent_trades}")
    room = config_.max_open_risk_percent - open_risk
    capacity_risk = requested
    fitted_capacity = False
    if config_.dynamic_risk_enabled and config_.dynamic_risk_fit_remaining:
        for candidate in dynamic_risk.fitting_tiers(config_, requested):
            if candidate > room + 1e-9:
                continue
            if equity is not None and not guardrails.projected_internal_daily_risk(
                    config_, state, equity, risk_basis, open_risk, candidate,
                    balance=balance):
                continue
            if balance is not None and not guardrails.projected_max_loss_risk(
                    config_, state, risk_basis, open_risk, candidate, balance):
                continue
            capacity_risk = candidate
            fitted_capacity = candidate != requested
            break
    risk_is_conditional = room + 1e-9 < capacity_risk
    if requests >= config_.max_requests_per_day * 0.9:
        blockers.append(f"requests {requests}/{config_.max_requests_per_day}")
    if equity is not None:
        projected = guardrails.projected_internal_daily_risk(
            config_, state, equity, risk_basis, open_risk, capacity_risk,
            balance=balance)
        if not projected:
            blockers.append(projected.reason)
    if balance is not None:
        projected_max = guardrails.projected_max_loss_risk(
            config_, state, risk_basis, open_risk, capacity_risk, balance)
        if not projected_max:
            blockers.append(projected_max.reason)

    allowed_side = "BUY/SELL"
    if positions and not config_.allow_opposing_positions:
        directions = {position.direction for position in positions}
        if len(directions) == 1:
            allowed_side = "BUY only" if 1 in directions else "SELL only"
        else:
            allowed_side = "none (mixed exposure)"
    if blockers:
        return False, "NO | " + "; ".join(blockers)
    if risk_is_conditional:
        return False, (
            f"CONDITIONAL | risk room {room:.2f}% < nominal "
            f"{requested:.2f}% | a rounded setup may fit only if actual "
            f"risk <= {room:.2f}% | allowed side {allowed_side}"
        )
    return True, (
        f"YES | capacity only | setups {active}/{config_.max_concurrent_trades} "
        f"| risk {open_risk:.2f}/{config_.max_open_risk_percent:.2f}% "
        f"| next {'fit ' if fitted_capacity else ''}{capacity_risk:.2f}% nominal "
        f"| allowed side {allowed_side}"
    )


def sleep_and_manage_split(broker: Broker, state: BotState, config_,
                           seconds: float) -> None:
    """Wait for the next signal scan while checking split exits separately.

    This does not load bars or evaluate signals. It only asks MT5 which managed
    positions remain, allowing `apply_breakeven` to react after TP1 instead of
    waiting as long as a full M15/M30 bar.
    """
    remaining = max(seconds, 0.0)
    while remaining > 0:
        fast = needs_split_management(state)
        budget_near_limit = (
            state.day_requests + broker.requests
            >= config_.max_requests_per_day * 0.9
        )
        interval = (min(config_.split_management_poll_seconds, remaining)
                    if fast and not budget_near_limit else remaining)
        time.sleep(interval)
        remaining -= interval
        if remaining <= 0 or not fast or budget_near_limit:
            continue
        # Ticket history is only needed while an order is unresolved. Once BE
        # is active, this loop remains alive to refresh negative swap but can do
        # that with one positions_get request.
        unresolved = any(
            trade.pending_tickets or trade.market_order_tickets
            for trade in state.open_trades()
        )
        if unresolved:
            trader.sync_fills(broker, state)
        live_tickets = trader.apply_breakeven(broker, state)
        finished_candidate = any(
            trade.position_tickets
            and not trade.pending_tickets
            and not trade.market_order_tickets
            and not any(ticket in live_tickets
                        for ticket in trade.position_tickets)
            for trade in state.open_trades()
            if trade.exit_mode == "be_33_33_34"
        )
        if finished_candidate:
            # Both survivors can hit their stops between signal bars. Score and
            # release the setup slot now instead of leaving stale exposure until
            # the next M15/M30 pass.
            trader.reconcile_closed(broker, state)
        checkpoint_state(broker, state)


PANEL_WIDTH = 100


def _panel_border(char: str = "-") -> None:
    print(paint("+" + char * (PANEL_WIDTH - 2) + "+", _Ansi.CYAN))


def _panel_row(label: str, value: str = "", value_style: str = _Ansi.WHITE) -> None:
    raw_label = f" {label:<15}" if label else " "
    value_width = PANEL_WIDTH - 2 - len(raw_label) - 1
    words = str(value).split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > value_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    lines.append(current)
    for index, line in enumerate(lines):
        visible_label = raw_label if index == 0 else " " * len(raw_label)
        visible_value = f" {line}".ljust(PANEL_WIDTH - 2 - len(visible_label))
        print(
            paint("|", _Ansi.CYAN)
            + paint(visible_label, _Ansi.BOLD, _Ansi.BLUE)
            + paint(visible_value, value_style)
            + paint("|", _Ansi.CYAN)
        )


def _panel_section(title: str) -> None:
    text = f" {title.upper()} "
    remaining = PANEL_WIDTH - 2 - len(text)
    print(
        paint("|", _Ansi.CYAN)
        + paint(text, _Ansi.BOLD, _Ansi.CYAN)
        + paint("-" * max(remaining, 0), _Ansi.DIM, _Ansi.CYAN)
        + paint("|", _Ansi.CYAN)
    )


def _panel_title(title: str, status: str) -> None:
    _panel_border("=")
    content = f" {title}"
    badge = f"[ {status} ] "
    gap = max(PANEL_WIDTH - 2 - len(content) - len(badge), 1)
    badge_colour = _Ansi.GREEN if "READY" in status else _Ansi.YELLOW
    print(
        paint("|", _Ansi.CYAN)
        + paint(content, _Ansi.BOLD, _Ansi.WHITE)
        + " " * gap
        + paint(badge, _Ansi.BOLD, badge_colour)
        + paint("|", _Ansi.CYAN)
    )
    _panel_border("=")


def _countdown(seconds: float) -> str:
    total = max(int(round(seconds)), 0)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def account_binding_status(state: BotState, account: dict) -> tuple[bool, str]:
    """Explain whether the loaded state can safely manage this MT5 account."""
    if account.get("is_hedging") is False:
        return False, (
            f"UNSUPPORTED | MT5 margin mode {account.get('margin_mode')} is "
            "netting | hedging required"
        )
    login, server = int(account.get("login") or 0), str(account.get("server") or "")
    if state.account_login is None:
        balance = float(account.get("balance") or 0.0)
        if state.initial_balance > 0 and balance > 0:
            difference = abs(balance - state.initial_balance) / state.initial_balance
            if difference > 0.25:
                return False, (
                    f"MISMATCH | legacy state {state.initial_balance:,.2f} vs "
                    f"MT5 {balance:,.2f} | archive state.json")
        return True, "UNBOUND | will bind to this login on the first LIVE pass"
    if state.account_login != login or state.account_server != server:
        return False, (
            f"MISMATCH | state {state.account_login}@{state.account_server} vs "
            f"MT5 {login}@{server}")
    return True, f"BOUND | {state.account_login}@{state.account_server}"


def print_status(broker: Broker, state: BotState, config_) -> None:
    account = broker.account()
    account_matches, binding = account_binding_status(state, account)
    positions = broker.positions()
    orders = broker.pending_orders()
    risk_basis = state.initial_balance or account["balance"]
    decision = dynamic_risk.decide(config_, state, account["equity"])
    risk = (open_risk_percent(broker.spec, positions, risk_basis)
            + pending_risk_percent(broker.spec, orders, risk_basis))
    progress = guardrails.progress(config_, state, account["equity"])

    offset, source = resolve_offset(broker, state, config_)
    calendar = news.load(config_)
    age = calendar.age_hours
    quote = broker.tick()
    now = quote["server_time"]
    upcoming = news.next_event(config_, now, offset, calendar)
    closure = market_hours.next_closure(config_, now)
    shut = market_hours.is_closed(config_, now)
    news_windows = news.windows(config_, offset, calendar)
    entry_window = guardrails.entry_window_open(
        config_, now, news_windows, calendar.usable)

    mode = "DRY RUN" if broker.dry_run else "LIVE"
    ready = ("READY" if account_matches and entry_window
             and not state.halted_forever else "GUARDED")
    print()
    _panel_title("QUANTUM DESK | EXECUTION MONITOR", f"{mode} | {ready}")

    _panel_section("Account")
    _panel_row("Account", f"{account['login']} @ {account['server']} ({account['currency']})")
    _panel_row("State", binding, _Ansi.GREEN if account_matches else _Ansi.RED)
    account_mode = (
        "HEDGING" if account.get("is_hedging") is True
        else "NETTING — UNSUPPORTED" if account.get("is_hedging") is False
        else "UNKNOWN"
    )
    _panel_row("Position mode", account_mode,
               _Ansi.GREEN if account.get("is_hedging") is True else _Ansi.RED)
    _panel_row("Balance", f"{account['balance']:,.2f}    Equity  {account['equity']:,.2f}    "
               f"Free margin  {account['margin_free']:,.2f}")
    _panel_row("Symbol", f"{broker.spec.name}    Digits {broker.spec.digits}    "
               f"Lot step {broker.spec.volume_step:g}    Min lot {broker.spec.volume_min:g}")

    _panel_section("Strategy and risk")
    _panel_row("Strategy", f"{config_.symbol}  {' + '.join(config_.timeframes)}    "
               f"Risk/trade {decision.risk_percent:.2f}%    "
               f"DD {decision.drawdown_percent:.2f}%    "
               f"Cap {config_.max_open_risk_percent:.2f}%    "
               f"Fit {'ON' if config_.dynamic_risk_fit_remaining else 'OFF'}")
    _panel_row("Exit", exit_mode_line(config_, risk_basis))
    _panel_row("Sizing", sizing_line(
        broker, config_, risk_basis, decision.risk_percent))
    _panel_row("Open risk", f"{risk:.2f}% of {risk_basis:,.2f} risk basis "
               "(positions + pending)")
    _panel_row("Safety", f"Kill switch {'ACTIVE' if KILL_SWITCH.exists() else 'OFF'}    "
               f"Requests {state.day_requests + broker.requests}/{config_.max_requests_per_day}")

    _panel_section("Performance")
    _panel_row("Target", f"Gain {progress['gain_percent']:.2f}% / {progress['target_percent']:.2f}%    "
               f"Progress {progress['target_progress']:.1f}%")
    _panel_row("Loss room", f"Daily {progress['daily_room_percent']:.2f}%    "
               f"Max loss {progress['max_loss_room_percent']:.2f}%    "
               f"Loss streak {progress['consecutive_losses']}")
    _panel_row("Trading days", str(progress['trading_days']))

    _panel_section("Market and schedule")
    _panel_row("Server time", f"{now:%Y-%m-%d %H:%M:%S}    UTC{offset:+g} ({source})")
    market_style = _Ansi.GREEN if not shut and entry_window else _Ansi.YELLOW
    _panel_row("Market", f"{'CLOSED' if shut else 'OPEN'}    "
               f"Entry gate {'OPEN' if entry_window else 'BLOCKED'}", market_style)
    if not entry_window:
        _panel_row("Blocked by", entry_window.reason, _Ansi.YELLOW)
    _panel_row("Next closure", f"{closure.start:%Y-%m-%d %H:%M} -> "
               f"{closure.end:%Y-%m-%d %H:%M}  {closure.label}")

    reference = frames_for_status(broker, config_)
    observed = market_hours.observed_week_end(reference)
    if observed is not None:
        day, moment = observed
        days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        configured = f"{days[config_.weekly_close_weekday]} {config_.weekly_close_hour:02d}:00"
        actual = f"{days[day]} {moment:%H:%M}"
        matches = (day == config_.weekly_close_weekday
                   and moment.hour == config_.weekly_close_hour
                   and moment.minute == 0)
        _panel_row("Week close", f"Observed {actual}    Configured {configured}    "
                   f"{'OK' if matches else 'CHECK SETTINGS'}")

    _panel_section("News")
    _panel_row("Calendar", f"{calendar.source}    {len(news.relevant(calendar, config_))} relevant    "
               f"Age {f'{age:.1f}h' if age is not None else 'n/a'}    "
               f"Window -{config_.news_minutes_before}/+{config_.news_minutes_after} min")
    if upcoming:
        event, moment = upcoming
        _panel_row("Next event", f"{moment:%Y-%m-%d %H:%M}  {event.currency}  {event.title}")
    else:
        _panel_row("Next event", "None in the current calendar")
    if calendar.error:
        _panel_row("News warning", calendar.error, _Ansi.YELLOW)

    _panel_section("Exposure")
    floating = floating_pnl(positions)
    _panel_row("Summary", f"Positions {len(positions)}    Pending {len(orders)}    "
               f"Floating P/L {floating:+,.2f}")
    _, capacity = entry_capacity(
        config_, state, positions, orders, broker.spec, risk_basis,
        state.day_requests + broker.requests, entry_window,
        setup_risk=decision.risk_percent, equity=account["equity"],
        balance=account["balance"])
    if not account_matches:
        capacity = f"NO | {binding}"
    _panel_row("Can open more", capacity,
               _Ansi.GREEN if capacity.startswith("YES") else _Ansi.YELLOW)
    if not positions:
        _panel_row("Positions", "None")
    for position in positions:
        trade = _trade_for_ticket(state, position.ticket)
        _panel_row(f"Position #{position.ticket}",
                   f"{'BUY' if position.direction == 1 else 'SELL'} {position.volume:g} @ "
                   f"{position.price_open} | SL {position.stop} | TP {position.take_profit} | "
                   f"Gross {position.profit:+.2f} | "
                   f"Swap {float(getattr(position, 'swap', 0.0)):+.2f} | "
                   f"Gross+Swap "
                   f"{position.profit + float(getattr(position, 'swap', 0.0)):+.2f}")
        if trade:
            _panel_row("Managed by",
                       f"{trade.plan_id} | {trade.exit_mode or 'legacy'} | "
                       f"{_target_name(trade, position.take_profit)}")
    if not orders:
        _panel_row("Pending", "None")
    for order in orders:
        expiry = order.get("expires_at")
        expiry_text = expiry.strftime("%Y-%m-%d %H:%M") if expiry else "GTC"
        _panel_row(f"Order #{order['ticket']}",
                   f"{order.get('type_name', order.get('type'))} {order.get('volume', 0):g} @ "
                   f"{order.get('price', 0)} | SL {order.get('stop', 0)} | "
                   f"TP {order.get('take_profit', 0)} | Exp {expiry_text}")

    live = journal.summarise(JOURNAL_PATH)
    _panel_section("Journal")
    _panel_row("Closed trades", str(live.get("trades", 0)))
    if live.get("trades", 0):
        _panel_row("Statistics", str(live))
    if state.halted_forever:
        _panel_row("HALTED", state.halted_reason, _Ansi.RED)
    _panel_border("=")
    print_management_alerts(state, positions, orders)
    for position in positions:
        print(f"[POSITION_HEALTH] "
              f"{position_health(position, state, quote, broker)}")
    for order in orders:
        print(f"[PENDING_HEALTH] {pending_health(order, state, now)}")
    print(f"[ENTRY_CAPACITY] {capacity}")
    print()

def _bind_and_anchor(broker: Broker, state: BotState, config_, *,
                     measure_offset: bool = True):
    """Bind state and establish its risk basis without evaluating a signal."""
    quote = broker.tick()
    account = broker.account()

    # The execution model needs separate position tickets for TP1/TP2/TP3 and
    # for concurrent M15/M30 setups. A netting account merges same-symbol orders
    # into one position, lets the last SL/TP overwrite the earlier legs, and
    # makes every ticket-based management invariant below false. Real Broker
    # instances always report this field; absence remains accepted for legacy
    # test/dry adapters that predate account-mode reporting.
    if account.get("is_hedging") is False:
        reason = (
            f"MT5 account margin mode {account.get('margin_mode')} is netting; "
            "Quantum Desk requires a hedging account"
        )
        journal.write(JOURNAL_PATH, "account_mode_blocked", reason=reason,
                      login=account.get("login"), server=account.get("server"))
        print(status_line("ACCOUNT_MODE_BLOCKED", reason, "error"))
        raise SystemExit(f"blocked: {reason}")

    try:
        newly_bound = state.bind_account(account)
    except ValueError as error:
        journal.write(JOURNAL_PATH, "account_mismatch", reason=str(error))
        print(status_line("ACCOUNT_MISMATCH", str(error), "error"))
        raise SystemExit(f"blocked: {error}") from error
    dirty = newly_bound
    if newly_bound:
        print(f"[ACCOUNT_BOUND] state -> {state.account_login}@{state.account_server}")

    if not state.initial_balance:
        state.initial_balance = config_.initial_balance or account["balance"]
        dirty = True
        print(f"[INIT] initial balance anchored at {state.initial_balance:.2f}")
    if state.observe_balance(account["balance"]):
        dirty = True
    if newly_bound:
        journal.write(JOURNAL_PATH, "account_bound",
                      login=state.account_login, server=state.account_server,
                      initial_balance=state.initial_balance)
    offset, _ = resolve_offset(
        broker, state, config_, quote=quote, measure=measure_offset)
    evaluation_day = ftmo_day(quote["server_time"], offset)
    day_balance = account["balance"]
    if state.day_key != evaluation_day.isoformat():
        cashflow_reader = getattr(broker, "account_cashflow_since", None)
        if cashflow_reader is not None:
            boundary = ftmo_day_start_server(quote["server_time"], offset)
            day_balance -= cashflow_reader(boundary)
    if state.roll_day(evaluation_day, day_balance, account["equity"]):
        dirty = True
        print(f"[DAY] {state.day_key} opens at balance {state.day_start_balance:.2f} "
              f"current equity {state.day_start_equity:.2f} "
              f"(reconstructed 00:00 CE(S)T balance reference)")
    if dirty:
        # Persist the high-water mark before any signal can use it. A crash must
        # not restart a drawdown account at the 1% tier.
        state.save(STATE_PATH)
    return quote, account


def reconcile_startup(broker: Broker, state: BotState, config_) -> None:
    """Adopt fills and catch up exit management after the bot was not running.

    A pending three-leg setup may fill between runs.  The old startup sequence
    printed a heartbeat and then slept until the next bar before calling
    ``sync_fills``; during that window all three live legs looked untracked and
    could not receive split-exit management. TP1 may also close while the
    computer is down, so break-even is reconciled here before any wait. This
    deliberately performs only account/state and order lifecycle work—never
    signal evaluation or entries.
    """
    _, account = _bind_and_anchor(broker, state, config_)
    trader.recover_orphan_setups(broker, state)
    # Protect already-mapped market positions before loading bars. A temporary
    # history/feed failure must not postpone a broker-side stop modification.
    trader.apply_breakeven(broker, state)
    unresolved_tickets = any(
        trade.market_order_tickets or trade.pending_tickets
        for trade in state.open_trades()
    )
    if unresolved_tickets:
        # Deal/order history does not need chart bars. Recover identities first
        # so a hard placement crash can still reach BE while the price-history
        # channel is unavailable.
        trader.sync_fills(broker, state)
        trader.apply_breakeven(broker, state)
    # Positions may have hit TP/SL while the process was down. Score them before
    # loading chart bars so startup capacity and loss-streak state are correct
    # immediately, even if the price-history channel is unavailable.
    trader.reconcile_closed(broker, state)
    frames = {timeframe: broker.bars(timeframe, config_.history_bars)
              for timeframe in config_.timeframes}
    trader.sync_fills(broker, state, frames)
    # A pending TP1 may only become identifiable after sync_fills maps its order
    # ticket to the resulting position ticket, so check once more after sync.
    trader.apply_breakeven(broker, state)
    checkpoint_state(broker, state)
    journal.write(
        JOURNAL_PATH, "startup_sync",
        login=account["login"], server=account["server"],
        managed_trades=len(state.open_trades()),
        position_tickets=[ticket for trade in state.open_trades()
                          for ticket in trade.position_tickets],
        pending_tickets=[ticket for trade in state.open_trades()
                         for ticket in trade.pending_tickets],
        market_order_tickets=[ticket for trade in state.open_trades()
                              for ticket in trade.market_order_tickets],
    )
    print("[STARTUP_SYNC] existing positions, pending orders, and break-even "
          "reconciled before signal loop")


def _record_missed_entry(state: BotState, intent: signals.Intent,
                         *, dry_run: bool = False) -> None:
    """Persist one non-actionable plan that appeared after its entry bar."""
    journal.write(
        JOURNAL_PATH, "entry_missed",
        plan_id=intent.plan_id, timeframe=intent.timeframe,
        side=intent.side, bars_since_signal=intent.bars_since_signal,
        reason="entry bar already passed; late entry not sent",
        dry_run=dry_run,
    )
    print(f"[MISSED_ENTRY] {intent.plan_id}: entry bar already passed; "
          "late entry not sent")
    state.remember_plan(intent.plan_id)


def _first_blocked(*checks):
    """Evaluate guard checks in order and stop before unnecessary broker reads."""
    for check in checks:
        verdict = check()
        if not verdict:
            return verdict
    return None


def _record_entry_block(state: BotState, intent: signals.Intent,
                        blocked, *, remember_plan: bool = False) -> None:
    """Persist and display a blocked plan, optionally retiring it for the day."""
    journal.write(JOURNAL_PATH, "entry_blocked", plan_id=intent.plan_id,
                  reason=blocked.reason, after_conversion=intent.converted)
    if remember_plan:
        state.remember_plan(intent.plan_id)
    note = " scope='limit already cancelled'" if intent.converted else ""
    print(f"[GUARD] {intent.plan_id}: {blocked.reason}{note}")


def pass_once(broker: Broker, state: BotState, config_, *,
              scan_timeframes: tuple[str, ...] | None = None,
              measure_offset: bool = False,
              manage_exposure: bool | None = None) -> None:
    quote, account = _bind_and_anchor(
        broker, state, config_, measure_offset=measure_offset)

    # Housekeeping first: an open trade must be managed even when new entries are off.
    # Reuse one positions/orders snapshot across the lifecycle checks. The old
    # path queried both repeatedly even when state had no live trade at all.
    managed_before = bool(state.open_trades()) or bool(manage_exposure)
    positions_snapshot = broker.positions() if managed_before else []
    orders_snapshot = broker.pending_orders() if managed_before else []
    unresolved_tickets = managed_before and any(
        trade.market_order_tickets or trade.pending_tickets
        for trade in state.open_trades()
    )
    if managed_before:
        # Protect already-mapped positions before loading chart history.
        trader.apply_breakeven(
            broker, state, positions=positions_snapshot)

    selected_timeframes = tuple(scan_timeframes or config_.timeframes)
    frames = {
        timeframe: broker.bars(timeframe, config_.history_bars)
        for timeframe in selected_timeframes
    }

    if managed_before:
        trader.sync_fills(
            broker, state, frames, orders=orders_snapshot)
        if unresolved_tickets:
            # A pending/market order may have become a position while history
            # was loading. Refresh once only for that transition path.
            positions_snapshot = broker.positions()
            orders_snapshot = broker.pending_orders()
            trader.apply_breakeven(
                broker, state, positions=positions_snapshot)
        trader.enforce_timeout(
            broker, state, frames, positions=positions_snapshot)
        trader.enforce_orphan_timeout(
            broker, state, frames, positions=positions_snapshot,
            configured_timeframes=config_.timeframes)
        trader.reconcile_closed(
            broker, state, positions=positions_snapshot,
            orders=orders_snapshot)

    # Reconciliation may have closed a winning or losing setup. Refresh before
    # sizing: using the pre-reconciliation equity could select a risk tier that
    # is too high after a loss. Persist a new closed-balance high-water before
    # any new entry is considered.
    if managed_before:
        account = broker.account()
    if state.observe_balance(account["balance"]):
        state.save(STATE_PATH)

    health = guardrails.account_health(config_, state, account["equity"], account["balance"])
    if not health:
        print(f"[GUARD] {health.reason}")
        # Only a breached max loss justifies closing at market. A daily pause or a
        # loss streak stops *new* entries; force-closing what is already running
        # would exit trades the backtest holds — including one sitting at
        # break-even after TP1 — and turn a pause into an unmeasured exit policy.
        if health.fatal:
            trader.flatten_all(broker, state, health.reason[:31])
            state.save(STATE_PATH)
            raise SystemExit(f"halted: {health.reason}")
        state.save(STATE_PATH)
        return

    # Every risk figure — the per-trade size and both exposure caps — is a
    # percentage of the *initial* balance, which is what the study's 0.40% ceiling
    # was derived from (10% max loss / 22R worst drawdown). Sizing off the live
    # balance instead would quietly scale risk up with every winning week, so the
    # cap it was chosen to respect would no longer hold.
    risk_basis = state.initial_balance or account["balance"]
    risk_decision = dynamic_risk.decide(config_, state, account["equity"])
    setup_risk = risk_decision.risk_percent
    if config_.dynamic_risk_enabled:
        print(f"[DYNAMIC_RISK] dd={risk_decision.drawdown_percent:.2f}% "
              f"risk={setup_risk:.2f}% "
              f"high_water={risk_decision.high_water_balance:.2f}")

    # Loaded once per pass: the calendar is cached on disk and the feed is only
    # re-fetched when the cache goes stale.
    offset, _ = resolve_offset(broker, state, config_, measure=False)
    calendar = news.load(config_)
    news_windows = news.windows(config_, offset, calendar)
    if calendar.error:
        print(f"[NEWS] {calendar.error}")

    for timeframe in selected_timeframes:
        frame = frames[timeframe]
        intent = signals.read(frame, timeframe)
        if intent is None:
            continue
        # `seen_plan_ids` is the belt to `trades`' braces. A plan skipped for
        # slippage is recorded there and nowhere else, and relying on the plan
        # ageing into a "wait" intent to stop a retry is one strategy change away
        # from re-entering a trade that was deliberately passed on.
        known = intent.plan_id in state.trades or intent.plan_id in state.seen_plan_ids

        if intent.action == "cancel":
            if known:
                trader.cancel_stale(broker, state, intent)
            continue
        if intent.converted:
            # The working limit belongs to this same plan, so `known` is true and
            # the guard below would skip it. Release the order first; only a
            # confirmed release lets this fall through to the entry path, which
            # then re-checks every guardrail against the new stop distance.
            if not trader.release_for_conversion(broker, state, intent):
                continue
        elif intent.action == signals.WAIT:
            # The strategy still labels a plan active after its entry bar, but a
            # process that was asleep or disconnected may have no corresponding
            # broker order. Entering it late would be a different trade from the
            # backtest. Record that distinction once instead of silently looking
            # as though the bot ignored a valid fresh signal.
            if not known:
                _record_missed_entry(state, intent, dry_run=broker.dry_run)
            continue
        elif known:
            continue

        request_block = guardrails.request_budget(
            config_, state.day_requests + broker.requests)
        if not request_block:
            # This plan's entry bar will be stale before the next server day.
            # Retiring it prevents repeated account/exposure/sizing reads from
            # burning the remaining allowance while the guard is already no.
            _record_entry_block(state, intent, request_block,
                                remember_plan=True)
            continue

        # A previous timeframe in this same pass may just have opened at market.
        # Refresh before each candidate so commission, floating P/L, free margin,
        # and a newly crossed dynamic-risk tier cannot remain stale for the next
        # setup.
        account = broker.account()
        if state.observe_balance(account["balance"]):
            state.save(STATE_PATH)
        current_health = guardrails.account_health(
            config_, state, account["equity"], account["balance"])
        if not current_health:
            print(f"[GUARD] {current_health.reason}")
            if current_health.fatal:
                trader.flatten_all(broker, state, current_health.reason[:31])
                state.save(STATE_PATH)
                raise SystemExit(f"halted: {current_health.reason}")
            state.save(STATE_PATH)
            return
        refreshed_decision = dynamic_risk.decide(
            config_, state, account["equity"])
        if refreshed_decision.risk_percent != setup_risk:
            print(f"[DYNAMIC_RISK_UPDATE] dd={refreshed_decision.drawdown_percent:.2f}% "
                  f"risk={refreshed_decision.risk_percent:.2f}%")
        setup_risk = refreshed_decision.risk_percent

        positions = broker.positions()
        orders = broker.pending_orders()
        live_risk = (
            open_risk_percent(broker.spec, positions, risk_basis)
            + pending_risk_percent(broker.spec, orders, risk_basis)
        )
        # Price this specific trade before asking whether it fits: the lot step
        # makes the real figure differ from `risk_percent`. With fit-remaining
        # enabled, a second setup may step down to the largest configured tier
        # whose rounded risk fits all three risk budgets.
        selected_risk, proposed = _fit_dynamic_setup_risk(
            broker, config_, state, intent, risk_basis, account["equity"],
            account["balance"], positions, orders, live_risk, setup_risk)
        if selected_risk != setup_risk:
            print(f"[DYNAMIC_RISK_FIT] plan={intent.plan_id} "
                  f"requested={setup_risk:.2f}% selected={selected_risk:.2f}% "
                  f"actual={proposed:.2f}% live={live_risk:.2f}%")
        setup_risk = selected_risk
        blocked = _first_blocked(
            lambda: guardrails.entry_window_open(
                config_, quote["server_time"], news_windows, calendar.usable),
            lambda: guardrails.can_open(
                config_, state,
                live_risk,
                len(positions), len(orders),
                state.day_requests + broker.requests,
                proposed_risk=proposed,
                active_setups=active_setup_count(state, positions, orders)),
            lambda: guardrails.projected_internal_daily_risk(
                config_, state, account["equity"], risk_basis,
                live_risk, proposed, balance=account["balance"]),
            lambda: guardrails.projected_max_loss_risk(
                config_, state, risk_basis, live_risk, proposed,
                account["balance"]),
            lambda: guardrails.no_opposing_position(
                config_, positions, intent.direction),
            lambda: guardrails.risk_per_idea(
                config_, broker.spec, positions, intent.direction,
                risk_basis, proposed_risk=proposed, orders=orders),
            lambda: guardrails.margin_available(
                account, broker.margin_for(intent.direction, _largest_leg(
                    broker, config_, intent, risk_basis, setup_risk)),
                len(config_.leg_weights_for(risk_basis))),
        )
        if blocked is not None:
            # Say when the block lands after a conversion. The limit is already
            # cancelled at this point, so the outcome is not "trade skipped" but
            # "trade given up" — an order that might still have filled is gone.
            # Reordering the guards ahead of the cancel would trade this away for
            # a worse bug: they would then count the very order being replaced as
            # live exposure and refuse the conversion on the account's own limits.
            _record_entry_block(state, intent, blocked)
            continue
        if intent.action == "market":
            price = quote["ask"] if intent.direction == 1 else quote["bid"]
            slippage = guardrails.entry_price_acceptable(
                config_, intent.direction, intent.entry, intent.risk, price)
            if not slippage:
                journal.write(JOURNAL_PATH, "entry_skipped", plan_id=intent.plan_id,
                              reason=slippage.reason)
                print(f"[SKIP] {intent.plan_id}: {slippage.reason}")
                state.remember_plan(intent.plan_id)
                continue
        trader.open_trade(broker, config_, state, intent, risk_basis,
                          risk_percent=setup_risk)

    trader.prune(state)
    checkpoint_state(broker, state)


def _seconds_to_next_close(server_time, timeframes) -> float:
    """Time until the soonest bar close on any traded timeframe."""
    waits = []
    for timeframe in timeframes:
        step = config.TIMEFRAME_SECONDS[timeframe.upper()]
        midnight = server_time.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed = (server_time - midnight).total_seconds()
        waits.append(step - elapsed % step)
    return min(waits)


def _timeframes_closing_at(server_time, timeframes) -> tuple[str, ...]:
    """Return only the configured bars that close at ``server_time``.

    The loop still wakes every 15 minutes for M15+M30, but an M30 history
    request is made only on :00/:30. This preserves signal cadence without
    spending a request on an unchanged M30 candle at :15/:45.
    """
    midnight = server_time.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = round((server_time - midnight).total_seconds())
    return tuple(
        timeframe for timeframe in timeframes
        if elapsed % config.TIMEFRAME_SECONDS[timeframe.upper()] == 0
    )


def loop(broker: Broker, state: BotState, config_) -> None:
    mode = "LIVE" if not broker.dry_run else "DRY-RUN"
    account = broker.account()
    journal.write(JOURNAL_PATH, "bot_started", mode=mode,
                  login=account["login"], server=account["server"],
                  symbol=config_.symbol, timeframes=list(config_.timeframes))
    print(status_line("RUN",
                      f"mode={paint(mode, _Ansi.BOLD, _Ansi.MAGENTA if mode == 'LIVE' else _Ansi.YELLOW)} "
                      f"symbol={config_.symbol} timeframes={'+'.join(config_.timeframes)} | "
                      "Ctrl+C to stop",
                      "live" if mode == "LIVE" else "warn"))
    reconnect_failures = 0
    feed_was_stale = False
    while True:
        try:
            # Sleeping to the next bar close instead of polling keeps the day's
            # terminal requests far below the limit an EA is allowed.
            quote = broker.tick()
            server_time = quote["server_time"]
            wait = _seconds_to_next_close(server_time, config_.timeframes)
            close_time = server_time + timedelta(seconds=wait)
            scan_timeframes = _timeframes_closing_at(
                close_time, config_.timeframes)
            sleep_seconds = wait + config_.entry_grace_seconds
            next_check = server_time + timedelta(seconds=sleep_seconds)
            # Once a frozen feed is confirmed, only one tick is needed to see
            # whether it recovered. Re-reading account/exposure every five
            # minutes while the market is closed used 1,152 requests per day.
            stale = broker.feed_stale_minutes(quote=quote)
            if stale is not None and feed_was_stale:
                print(status_line(
                    "HEARTBEAT",
                    f"{server_time:%Y-%m-%d %H:%M:%S} SERVER | "
                    f"FEED STALE {stale:.0f}m | quote-only recheck in "
                    f"{_countdown(STALE_FEED_RECHECK_SECONDS)}",
                    "warn",
                ), flush=True)
                journal.write(
                    JOURNAL_PATH, "heartbeat", mode=mode,
                    server_time=server_time, feed_stale_minutes=round(stale, 1),
                    entry_capacity="NO | feed stale; quote-only recheck",
                )
                checkpoint_state(broker, state)
                time.sleep(STALE_FEED_RECHECK_SECONDS)
                continue
            positions = broker.positions()
            orders = broker.pending_orders()
            account = broker.account()
            floating = floating_pnl(positions)
            exposure_level = "ok" if not positions and not orders else "live"
            # A frozen feed produced heartbeats that looked entirely normal while
            # the bot could not see a bar close at all. Say so instead.
            heartbeat = (
                f"{server_time:%Y-%m-%d %H:%M:%S} SERVER | "
                f"{'LIVE' if not broker.dry_run else 'DRY RUN'} | "
                f"POS {len(positions)} | PENDING {len(orders)} | "
                f"FLOAT {floating:+,.2f} | NEXT {next_check:%H:%M:%S} "
                f"IN {_countdown(sleep_seconds)}"
            )
            if stale is not None:
                heartbeat += f" | FEED STALE {stale:.0f}m"
                exposure_level = "warn"
            print(status_line("HEARTBEAT", heartbeat, exposure_level), flush=True)
            for position in positions:
                print(f"[POSITION] {trader.describe(position)}")
                print(f"[POSITION_HEALTH] "
                      f"{position_health(position, state, quote, broker)}")
            for order in orders:
                print(f"[PENDING] {describe_order(order)}")
                print(f"[PENDING_HEALTH] {pending_health(order, state, server_time)}")
            risk_basis = state.initial_balance or config_.initial_balance
            if risk_basis:
                heartbeat_risk = dynamic_risk.decide(
                    config_, state, account["equity"]).risk_percent
                _, capacity = entry_capacity(
                    config_, state, positions, orders, broker.spec, risk_basis,
                    state.day_requests + broker.requests,
                    setup_risk=heartbeat_risk, equity=account["equity"],
                    balance=account["balance"])
                if stale is not None:
                    capacity = (f"NO | feed stale {stale:.0f}m; "
                                "waiting for an advancing quote")
                print(f"[ENTRY_CAPACITY] {capacity}")
            else:
                print("[ENTRY_CAPACITY] NO | initial balance not anchored yet")
                capacity = "NO | initial balance not anchored yet"
            journal.write(JOURNAL_PATH, "heartbeat", mode=mode,
                          server_time=server_time, positions=len(positions),
                          pending_orders=len(orders), floating_pnl=round(floating, 2),
                          entry_capacity=capacity)
            print_management_alerts(state, positions, orders)

            # A frozen quote cannot produce a new closed bar. Calling pass_once
            # here used to ask MT5 for history anyway; over the weekend that call
            # could remain blocked until Monday, so the first live signal after
            # reopen was already several bars old when the loop returned. Fast
            # split polling also burned the terminal request allowance while no
            # fill or BE trigger could occur. Broker-side SL/TP remain active, so
            # checkpoint reads already made by this heartbeat and wait for a new
            # tick before touching history again.
            if stale is not None:
                if not feed_was_stale:
                    journal.write(JOURNAL_PATH, "feed_stale",
                                  server_time=server_time,
                                  stale_minutes=round(stale, 1))
                feed_was_stale = True
                checkpoint_state(broker, state)
                time.sleep(STALE_FEED_RECHECK_SECONDS)
                continue

            # Scan immediately on the first advancing tick. Waiting for the next
            # scheduled close here can age the newest closed bar from fresh to
            # stale and lose exactly the entry the recovery was meant to catch.
            if feed_was_stale:
                feed_was_stale = False
                print(status_line(
                    "FEED_RESTORED",
                    "quotes are advancing again | scanning closed bars now",
                    "ok"))
                journal.write(JOURNAL_PATH, "feed_restored",
                              server_time=server_time)
                pass_once(
                    broker, state, config_, measure_offset=True,
                    manage_exposure=bool(positions or orders))
                reconnect_failures = 0
                continue

            sleep_and_manage_split(broker, state, config_, sleep_seconds)
            pass_once(
                broker, state, config_, scan_timeframes=scan_timeframes,
                manage_exposure=bool(positions or orders))
            reconnect_failures = 0
        except OrderRejected as error:
            # A broker refusal used to be called a lost connection, which spent
            # every pass reconnecting while the rejected ticket stayed exposed.
            checkpoint_state(broker, state)
            print(status_line("ORDER_REJECTED", str(error), "warn"))
            journal.write(JOURNAL_PATH, "order_rejected", reason=str(error))
            continue
        except MT5Error as error:
            # Broker-side SL/TP and pending orders survive this process losing
            # connectivity. What stops is observation, BE moves, timeouts and new
            # entries. Reinitialize MT5 with bounded backoff, then reconcile state
            # before normal scanning resumes; never resend an existing order.
            checkpoint_state(broker, state)
            delay = min(
                config_.reconnect_initial_seconds * (2 ** min(reconnect_failures, 10)),
                config_.reconnect_max_seconds,
            )
            print(status_line(
                "CONNECTION_LOST",
                f"{error} | broker-side SL/TP remain active | reconnect in {delay}s",
                "error"))
            journal.write(JOURNAL_PATH, "connection_lost",
                          reason=str(error), retry_seconds=delay)
            time.sleep(delay)
            try:
                broker.reconnect()
            except MT5Error as reconnect_error:
                reconnect_failures += 1
                print(status_line(
                    "RECONNECT_WAIT",
                    f"{reconnect_error} | next retry will use bounded backoff",
                    "warn"))
                continue
            reconnect_failures = 0
            print(status_line(
                "CONNECTION_RESTORED",
                "MT5 reconnected | reconciling positions and pending orders",
                "ok"))
            journal.write(JOURNAL_PATH, "connection_restored")
            try:
                pass_once(broker, state, config_, measure_offset=True)
            except MT5Error as sync_error:
                checkpoint_state(broker, state)
                print(status_line(
                    "RECONCILE_WAIT",
                    f"reconnected but first sync is incomplete: {sync_error}",
                    "warn"))
        except KeyboardInterrupt:
            journal.write(JOURNAL_PATH, "bot_stopped", mode=mode,
                          reason="keyboard interrupt")
            print("\n" + status_line(
                "STOP", "interrupted; open positions keep their broker-side SL/TP", "warn"))
            return


def execute(*, live: bool = False, once: bool = False, status: bool = False,
            flatten: bool = False, reconcile_only: bool = False) -> None:
    """Open a session and do one thing with it.

    Separate from `main` so `bot.main`'s menu can ask for an action directly
    rather than assembling command-line strings and re-parsing them.
    """
    instance_lock = LiveInstanceLock(LIVE_LOCK_PATH) if live else None
    if instance_lock is not None and not instance_lock.acquire():
        message = "another Quantum Desk LIVE process is already running"
        print(status_line("BLOCKED", message, "error"))
        raise SystemExit(f"blocked: {message}")
    previous_journal_enabled = journal.set_enabled(live)
    try:
        config_ = settings_module.load()
        state = BotState.load(STATE_PATH)
        if not live:
            # Dry-run may mutate its in-memory copy while simulating lifecycle
            # events, but it must never mark production plans seen/closed or
            # race a live process by replacing state.json.
            state.disable_persistence()

        with Broker(config_.symbol, config_.magic, config_.deviation_points,
                    dry_run=not live,
                    write_spacing_seconds=config_.write_spacing_seconds) as broker:
            if status:
                print_status(broker, state, config_)
                account = broker.account()
                journal.write(JOURNAL_PATH, "status_checked", mode="DRY-RUN",
                              login=account["login"], server=account["server"])
                return
            if flatten:
                trader.flatten_all(broker, state, "manual flatten")
                state.save(STATE_PATH)
                return
            reconcile_startup(broker, state, config_)
            print_status(broker, state, config_)
            if reconcile_only:
                print(status_line(
                    "RECONCILED",
                    "startup state and broker exposure checked; no signal pass",
                    "ok",
                ))
                state.save(STATE_PATH)
                return
            if once:
                pass_once(broker, state, config_)
            else:
                loop(broker, state, config_)
            state.save(STATE_PATH)
    finally:
        journal.set_enabled(previous_journal_enabled)
        if instance_lock is not None:
            instance_lock.release()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true",
                        help="send real orders (default is dry-run)")
    parser.add_argument("--once", action="store_true", help="run a single pass and exit")
    parser.add_argument("--status", action="store_true", help="print status and exit")
    parser.add_argument(
        "--reconcile", action="store_true",
        help="reconcile broker/state and exit without evaluating signals",
    )
    parser.add_argument("--flatten", action="store_true",
                        help="cancel every order and close every position, then exit")
    args = parser.parse_args(argv)
    execute(live=args.live, once=args.once, status=args.status,
            flatten=args.flatten, reconcile_only=args.reconcile)


if __name__ == "__main__":
    main()
