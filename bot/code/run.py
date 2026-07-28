"""Bot entry point.

    python -m bot.code.run --status          # account, guards and live R stats
    python -m bot.code.run --once            # one pass, dry-run
    python -m bot.code.run                   # loop, dry-run (safe default)
    python -m bot.code.run --live            # loop, sends real orders
    python -m bot.code.run --flatten --live  # emergency: cancel and close everything

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

from xau import config
from xau.mt5_source import MT5Error

from . import (guardrails, journal, market_hours, news,
               settings as settings_module, signals, trader)
from .broker import Broker
from .settings import JOURNAL_PATH, KILL_SWITCH, STATE_PATH
from .sizing import open_risk_percent
from .state import BotState


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


def resolve_offset(broker: Broker, state: BotState, config_) -> tuple[float, str]:
    """Server-to-UTC offset: measured if quotes are live, else last known, else config."""
    measured = broker.server_utc_offset()
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
    "be_33_33_34": "three legs 33/33/34, BE after TP1",
    "auto": "split when the size allows (NOT RECOMMENDED)",
}
#: `exit_mode` value that corresponds to each technique the lab can select.
TECHNIQUE_TO_MODE = {"fixed_tp3": "fixed_tp3",
                     "be_after_tp1_33_33_34": "be_33_33_34"}


def exit_mode_line(config_) -> str:
    """The exit in force, checked against what the study selected for each TF.

    The point of naming the exit was to stop the account balance choosing it. The
    other half of that is checking the name still matches the research: a report
    regenerated with fresh data can select a different technique, and nothing
    would otherwise tell the operator that live and backtest had parted company.
    """
    label = EXIT_LABELS.get(config_.exit_mode, config_.exit_mode)
    try:
        from xau import backtest_reporting as br

        mismatched = []
        for timeframe in config_.timeframes:
            path = br.report_path(config_.symbol, timeframe)
            if not path.exists():
                continue
            picked = br.select_technique(br.load_report(path))
            if TECHNIQUE_TO_MODE.get(picked, picked) != config_.exit_mode:
                mismatched.append(f"{timeframe} wants {picked}")
        if mismatched:
            return f"{config_.exit_mode} — {label}    <-- CHECK: {', '.join(mismatched)}"
        return f"{config_.exit_mode} — {label}    matches the study"
    except Exception as error:                    # noqa: BLE001 - reporting only
        return f"{config_.exit_mode} — {label}    (could not check: {error})"


def sizing_line(broker: Broker, config_, balance: float) -> str:
    """What a typical trade will really be sized at, per traded timeframe.

    The lot step only rounds down, so on a small account the position can carry
    materially less risk than the settings ask for — 0.40% of 10,000 wants 0.0191
    lots of gold and can only send 0.01, which is 0.21%. That gap is worth seeing
    before it turns up as a suspiciously good expectancy in the journal.
    """
    from .sizing import SizingError, size_plan

    parts = []
    for timeframe in config_.timeframes:
        stop = _typical_stop(broker, config_, timeframe)
        if stop is None:
            parts.append(f"{timeframe} ?")
            continue
        try:
            s = size_plan(broker.spec, balance, config_.risk_percent, stop,
                          config_.leg_weights,
                          rounding=config_.lot_rounding,
                          max_overshoot=config_.max_risk_overshoot)
        except SizingError:
            parts.append(f"{timeframe} REFUSED (stop {stop:.2f})")
            continue
        actual_pct = s.risk_cash / balance * 100 if balance else 0.0
        lots = "+".join(f"{leg:g}" for leg in s.legs)
        flag = "" if s.risk_shortfall >= 0.85 else f"!{s.risk_shortfall:.0%}"
        parts.append(f"{timeframe} {lots}lot={actual_pct:.2f}%{flag}")
    asked = f"asked {config_.risk_percent:.2f}%"
    return f"{asked}   " + "   ".join(parts)


def _typical_stop(broker: Broker, config_, timeframe: str) -> float | None:
    """Median stop distance over recent bars, for the sizing preview only."""
    from xau import quantum

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


def _proposed_risk_percent(broker: Broker, config_, intent, balance: float) -> float | None:
    """Risk this plan will really carry, as a percent of the risk basis.

    None when the plan cannot be sized at all — the caller should let the sizing
    step produce the proper refusal rather than have the exposure cap guess.
    """
    from .sizing import SizingError, size_plan

    if balance <= 0:
        return None
    try:
        sizing = size_plan(broker.spec, balance, config_.risk_percent, intent.risk,
                           config_.leg_weights, rounding=config_.lot_rounding,
                           max_overshoot=config_.max_risk_overshoot)
    except SizingError:
        return None
    return sizing.risk_cash / balance * 100


def _largest_leg(broker: Broker, config_, intent, balance: float) -> float:
    """Volume of the biggest leg, for the margin question. 0 if unsizeable."""
    from .sizing import SizingError, size_plan

    try:
        return max(size_plan(broker.spec, balance, config_.risk_percent,
                             intent.risk, config_.leg_weights,
                             rounding=config_.lot_rounding,
                             max_overshoot=config_.max_risk_overshoot).legs)
    except SizingError:
        return 0.0


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


def print_exposure(positions, orders, *, label: str = "EXPOSURE") -> None:
    floating = sum(position.profit for position in positions)
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

PANEL_WIDTH = 100


def _panel_border(char: str = "-") -> None:
    print(paint("+" + char * (PANEL_WIDTH - 2) + "+", _Ansi.CYAN))


def _panel_row(label: str, value: str = "", value_style: str = _Ansi.WHITE) -> None:
    raw_label = f" {label:<15}" if label else " "
    raw_value = f" {value}"
    content = (raw_label + raw_value)[:PANEL_WIDTH - 2].ljust(PANEL_WIDTH - 2)
    visible_label = content[:len(raw_label)]
    visible_value = content[len(raw_label):]
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

def print_status(broker: Broker, state: BotState, config_) -> None:
    account = broker.account()
    positions = broker.positions()
    orders = broker.pending_orders()
    risk_basis = state.initial_balance or account["balance"]
    risk = open_risk_percent(broker.spec, positions, risk_basis)
    progress = guardrails.progress(config_, state, account["equity"])

    offset, source = resolve_offset(broker, state, config_)
    calendar = news.load(config_)
    age = calendar.age_hours
    now = broker.tick()["server_time"]
    upcoming = news.next_event(config_, now, offset, calendar)
    closure = market_hours.next_closure(config_, now)
    shut = market_hours.is_closed(config_, now)
    news_windows = news.windows(config_, offset, calendar)
    entry_window = guardrails.entry_window_open(
        config_, now, news_windows, calendar.usable)

    mode = "DRY RUN" if broker.dry_run else "LIVE"
    ready = "READY" if entry_window and not state.halted_forever else "GUARDED"
    print()
    _panel_title("QUANTUM DESK | EXECUTION MONITOR", f"{mode} | {ready}")

    _panel_section("Account")
    _panel_row("Account", f"{account['login']} @ {account['server']} ({account['currency']})")
    _panel_row("Balance", f"{account['balance']:,.2f}    Equity  {account['equity']:,.2f}    "
               f"Free margin  {account['margin_free']:,.2f}")
    _panel_row("Symbol", f"{broker.spec.name}    Digits {broker.spec.digits}    "
               f"Lot step {broker.spec.volume_step:g}    Min lot {broker.spec.volume_min:g}")

    _panel_section("Strategy and risk")
    _panel_row("Strategy", f"{config_.symbol}  {' + '.join(config_.timeframes)}    "
               f"Risk/trade {config_.risk_percent:.2f}%    Cap {config_.max_open_risk_percent:.2f}%")
    _panel_row("Exit", exit_mode_line(config_))
    _panel_row("Sizing", sizing_line(broker, config_, risk_basis))
    _panel_row("Open risk", f"{risk:.2f}% of {risk_basis:,.2f} risk basis")
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
    floating = sum(position.profit for position in positions)
    _panel_row("Summary", f"Positions {len(positions)}    Pending {len(orders)}    "
               f"Floating P/L {floating:+,.2f}")
    if not positions:
        _panel_row("Positions", "None")
    for position in positions:
        _panel_row(f"Position #{position.ticket}",
                   f"{'BUY' if position.direction == 1 else 'SELL'} {position.volume:g} @ "
                   f"{position.price_open} | SL {position.stop} | TP {position.take_profit} | "
                   f"P/L {position.profit:+.2f}")
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
    print()

def pass_once(broker: Broker, state: BotState, config_) -> None:
    quote = broker.tick()
    account = broker.account()

    if not state.initial_balance:
        state.initial_balance = config_.initial_balance or account["balance"]
        print(f"[INIT] initial balance anchored at {state.initial_balance:.2f}")
    if state.roll_day(quote["server_time"].date(), account["balance"],
                      account["equity"]):
        print(f"[DAY] {state.day_key} opens at balance {state.day_start_balance:.2f} "
              f"equity {state.day_start_equity:.2f} (loss floor uses the higher)")

    # Housekeeping first: an open trade must be managed even when new entries are off.
    frames = {timeframe: broker.bars(timeframe, config_.history_bars)
              for timeframe in config_.timeframes}
    trader.sync_fills(broker, state, frames)
    trader.apply_breakeven(broker, state)
    trader.enforce_timeout(broker, state, frames)
    trader.reconcile_closed(broker, state)

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

    # Loaded once per pass: the calendar is cached on disk and the feed is only
    # re-fetched when the cache goes stale.
    offset, _ = resolve_offset(broker, state, config_)
    calendar = news.load(config_)
    news_windows = news.windows(config_, offset, calendar)
    if calendar.error:
        print(f"[NEWS] {calendar.error}")

    for timeframe in config_.timeframes:
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
        if intent.action == signals.WAIT or known:
            continue

        positions = broker.positions()
        # Price this specific trade before asking whether it fits: the lot step
        # makes the real figure differ from `risk_percent`, and the exposure cap
        # should be judged against what will actually be sent.
        proposed = _proposed_risk_percent(broker, config_, intent, risk_basis)
        checks = (
            guardrails.entry_window_open(config_, quote["server_time"],
                                         news_windows, calendar.usable),
            guardrails.can_open(
                config_, state,
                open_risk_percent(broker.spec, positions, risk_basis),
                len(positions), len(broker.pending_orders()),
                state.day_requests + broker.requests,
                proposed_risk=proposed),
            guardrails.no_opposing_position(config_, positions, intent.direction),
            guardrails.risk_per_idea(config_, broker.spec, positions, intent.direction,
                                     risk_basis),
            guardrails.margin_available(
                account, broker.margin_for(intent.direction, _largest_leg(
                    broker, config_, intent, risk_basis)), len(config_.leg_weights)),
        )
        blocked = next((check for check in checks if not check), None)
        if blocked is not None:
            journal.write(JOURNAL_PATH, "entry_blocked", plan_id=intent.plan_id,
                          reason=blocked.reason)
            print(f"[GUARD] {intent.plan_id}: {blocked.reason}")
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
        trader.open_trade(broker, config_, state, intent, risk_basis)

    trader.prune(state)
    state.day_requests += broker.take_requests()
    state.save(STATE_PATH)


def _seconds_to_next_close(server_time, timeframes) -> float:
    """Time until the soonest bar close on any traded timeframe."""
    waits = []
    for timeframe in timeframes:
        step = config.TIMEFRAME_SECONDS[timeframe.upper()]
        midnight = server_time.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed = (server_time - midnight).total_seconds()
        waits.append(step - elapsed % step)
    return min(waits)


def loop(broker: Broker, state: BotState, config_) -> None:
    mode = "LIVE" if not broker.dry_run else "DRY-RUN"
    print(status_line("RUN",
                      f"mode={paint(mode, _Ansi.BOLD, _Ansi.MAGENTA if mode == 'LIVE' else _Ansi.YELLOW)} "
                      f"symbol={config_.symbol} timeframes={'+'.join(config_.timeframes)} | "
                      "Ctrl+C to stop",
                      "live" if mode == "LIVE" else "warn"))
    while True:
        try:
            # Sleeping to the next bar close instead of polling keeps the day's
            # terminal requests far below the limit an EA is allowed.
            server_time = broker.tick()["server_time"]
            wait = _seconds_to_next_close(server_time, config_.timeframes)
            sleep_seconds = wait + config_.entry_grace_seconds
            next_check = server_time + timedelta(seconds=sleep_seconds)
            positions = broker.positions()
            orders = broker.pending_orders()
            floating = sum(position.profit for position in positions)
            exposure_level = "ok" if not positions and not orders else "live"
            # A frozen feed produced heartbeats that looked entirely normal while
            # the bot could not see a bar close at all. Say so instead.
            stale = broker.feed_stale_minutes()
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
            for order in orders:
                print(f"[PENDING] {describe_order(order)}")
            print_management_alerts(state, positions, orders)
            time.sleep(sleep_seconds)
            pass_once(broker, state, config_)
        except MT5Error as error:
            # A closed market or a dropped terminal link must not kill the run.
            print(status_line("WARN", str(error), "warn"))
            time.sleep(max(config_.poll_seconds, 60))
        except KeyboardInterrupt:
            print("\n" + status_line(
                "STOP", "interrupted; open positions keep their broker-side SL/TP", "warn"))
            return


def execute(*, live: bool = False, once: bool = False, status: bool = False,
            flatten: bool = False) -> None:
    """Open a session and do one thing with it.

    Separate from `main` so `bot.main`'s menu can ask for an action directly
    rather than assembling command-line strings and re-parsing them.
    """
    config_ = settings_module.load()
    state = BotState.load(STATE_PATH)

    with Broker(config_.symbol, config_.magic, config_.deviation_points,
                dry_run=not live) as broker:
        if status:
            print_status(broker, state, config_)
            return
        if flatten:
            trader.flatten_all(broker, state, "manual flatten")
            state.save(STATE_PATH)
            return
        print_status(broker, state, config_)
        if once:
            pass_once(broker, state, config_)
        else:
            loop(broker, state, config_)
        state.save(STATE_PATH)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true",
                        help="send real orders (default is dry-run)")
    parser.add_argument("--once", action="store_true", help="run a single pass and exit")
    parser.add_argument("--status", action="store_true", help="print status and exit")
    parser.add_argument("--flatten", action="store_true",
                        help="cancel every order and close every position, then exit")
    args = parser.parse_args(argv)
    execute(live=args.live, once=args.once, status=args.status, flatten=args.flatten)


if __name__ == "__main__":
    main()
