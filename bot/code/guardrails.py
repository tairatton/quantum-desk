"""The rules that decide whether a new trade is allowed at all.

Order matters: the FTMO hard limits are checked before the self-imposed ones, and
both are checked before anything about the signal. A guard never sizes down to
fit — it refuses, because a smaller position taken past a limit is still a rule
breach waiting to happen.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from . import market_hours
from .settings import KILL_SWITCH, Settings
from .state import BotState


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str
    fatal: bool = False    # True = stop the process, not just this trade

    def __bool__(self) -> bool:
        return self.allowed


ALLOWED = Verdict(True, "ok")


def account_health(settings: Settings, state: BotState, equity: float,
                   balance: float) -> Verdict:
    """FTMO objectives first, then the tighter internal stops."""
    if state.halted_forever:
        return Verdict(False, f"halted: {state.halted_reason}", fatal=True)

    initial = state.initial_balance or balance
    max_loss_floor = initial * (1 - settings.max_loss_percent / 100)
    if equity <= max_loss_floor:
        state.halt(f"max loss breached: equity {equity:.2f} <= {max_loss_floor:.2f}")
        return Verdict(False, state.halted_reason, fatal=True)

    # FTMO compares against the higher of balance and equity at the day's open,
    # not balance alone. Positions held overnight can put equity above balance,
    # which tightens the real floor — so the reference has to be the same one the
    # rule uses. `day_start_equity` is 0 on state written before it existed.
    day_reference = max(state.day_start_balance, state.day_start_equity)
    daily_floor = day_reference * (1 - settings.daily_loss_percent / 100)
    if day_reference and equity <= daily_floor:
        state.pause_today()
        return Verdict(False, f"daily loss limit hit: equity {equity:.2f} "
                              f"<= {daily_floor:.2f}")

    internal_floor = day_reference * (
        1 - settings.internal_daily_stop_percent / 100)
    if day_reference and equity <= internal_floor:
        state.pause_today()
        return Verdict(False, f"internal daily stop {settings.internal_daily_stop_percent:.2f}%"
                              f" hit at equity {equity:.2f}")

    # Passing is a reason to stop, not to carry on. Once the target and the four
    # trading days are both in, every further trade risks an objective for a
    # result that has already been earned — `progress()` worked this out and
    # nothing acted on it. Trading continues while the target is met but the day
    # count is not, because the evaluation is not complete until both are.
    if settings.stop_at_target:
        standing = progress(settings, state, equity)
        if standing["objectives_met"]:
            return Verdict(False, f"objectives met: {standing['gain_percent']:+.2f}% over "
                                  f"{standing['trading_days']} — stop trading and submit. "
                                  f"Set stop_at_target false to carry on.")

    if state.consecutive_losses >= settings.max_consecutive_losses:
        state.pause_today()
        return Verdict(False, f"{state.consecutive_losses} losses in a row; "
                              f"standing down until the next server day")
    if state.is_paused_today:
        return Verdict(False, "paused for the rest of this trading day")
    return ALLOWED


def can_open(settings: Settings, state: BotState, open_risk: float,
             open_count: int, pending_count: int, requests_today: int = 0,
             proposed_risk: float | None = None,
             active_setups: int | None = None) -> Verdict:
    """Exposure checks for one more entry.

    `proposed_risk` is what the next trade will really carry as a percentage,
    which is not `settings.risk_percent` once the lot step has had its say. This
    used to compare the room left against the nominal setting, so a trade sized at
    0.31% could be refused for lack of 0.40% of headroom — turning down a trade
    that fits.
    """
    if KILL_SWITCH.exists():
        return Verdict(False, f"kill switch present: {KILL_SWITCH.name}")
    exposure_count = (open_count + pending_count
                      if active_setups is None else active_setups)
    if exposure_count >= settings.max_concurrent_trades:
        if active_setups is not None:
            return Verdict(False, f"already managing {active_setups} active setup(s)")
        return Verdict(False, f"already holding {open_count} positions and "
                              f"{pending_count} pending orders")
    needs = settings.risk_percent if proposed_risk is None else proposed_risk
    room = settings.max_open_risk_percent - open_risk
    if room < needs:
        return Verdict(False, f"open risk {open_risk:.2f}% leaves {room:.2f}%, "
                              f"less than the {needs:.2f}% this trade needs")
    # Exceeding the terminal request allowance is a forbidden practice, and the
    # budget was previously only assumed to be safe rather than counted. Reads
    # still continue past this point — an open trade must be managed — but nothing
    # new is started on the last of the day's allowance.
    if requests_today >= settings.max_requests_per_day * 0.9:
        return Verdict(False, f"{requests_today} server requests today, near the "
                              f"{settings.max_requests_per_day} allowance")
    return ALLOWED


def margin_available(account: dict, margin_needed: float | None,
                     legs: int) -> Verdict:
    """Refuse a plan the account cannot actually carry.

    Gold on a Swing account is leveraged 1:9, and a rejection half-way through
    placing three legs is exactly the failure that used to strand a trade. Asking
    first turns it into a clean skip. An unanswerable margin query is allowed
    through: the broker gets the final say either way.
    """
    if margin_needed is None:
        return ALLOWED
    required = margin_needed * legs
    free = account.get("margin_free", 0.0)
    if required > free * 0.8:
        return Verdict(False, f"needs about {required:.2f} margin for {legs} leg(s) "
                              f"but only {free:.2f} is free")
    return ALLOWED


def no_opposing_position(settings: Settings, positions, direction: int) -> Verdict:
    """FTMO forbids holding opposing positions on the same instrument.

    M15 and M30 read the same market and can disagree. Rather than hedge — which
    is a forbidden practice, not just a bad idea — the later signal is dropped.
    """
    if settings.allow_opposing_positions:
        return ALLOWED
    against = [p for p in positions if p.direction != direction]
    if against:
        return Verdict(False, f"would oppose {len(against)} open position(s); "
                              f"hedging the same instrument is a forbidden practice")
    return ALLOWED


def risk_per_idea(settings: Settings, spec, positions, direction: int,
                  balance: float) -> Verdict:
    """Cap the risk carried by one trade idea.

    Two timeframes firing the same direction is one idea taken twice. FTMO
    reviews "risk per trade idea", so same-direction exposure is capped on its
    own, tighter than the overall open-risk cap.
    """
    from .sizing import open_risk_percent   # local import keeps the cycle out

    same_side = [p for p in positions if p.direction == direction]
    if not same_side:
        return ALLOWED
    used = open_risk_percent(spec, same_side, balance)
    if used + settings.risk_percent > settings.max_risk_per_idea_percent + 1e-9:
        return Verdict(False, f"same-direction risk {used:.2f}% + "
                              f"{settings.risk_percent:.2f}% exceeds the "
                              f"{settings.max_risk_per_idea_percent:.2f}% per-idea cap")
    return ALLOWED


def entry_window_open(settings: Settings, server_time: datetime,
                      news_windows: Sequence[tuple[datetime, datetime]] = (),
                      calendar_usable: bool = True) -> Verdict:
    """Block entries FTMO treats as gap trading, and any news blackout window.

    The forbidden practice is opening a position within two hours of a closure
    lasting two hours or more. `bot.market_hours` works out which closure is next
    — the weekly close, or a configured holiday or early close — so a Christmas
    Eve shutdown is handled by the same rule as a Friday night, rather than the
    weekday arithmetic this used to do, which knew about Fridays and nothing else.

    Only opening is blocked. A position already running is left alone to hold
    through the closure, which is what was measured.

    `news_windows` comes from `bot.news`, already on the broker's server clock, so
    this function stays pure and testable.
    """
    shut = market_hours.is_closed(settings, server_time)
    if shut is not None:
        return Verdict(False, f"market is closed: {shut.label} until "
                              f"{shut.end:%a %d %b %H:%M} server time")
    closing = market_hours.entry_blackout(settings, server_time)
    if closing is not None:
        hours_left = (closing.start - server_time).total_seconds() / 3600
        return Verdict(False, f"{hours_left:.1f}h to {closing.label} "
                              f"({closing.hours:.0f}h shut); inside the "
                              f"{settings.blackout_hours_before_close:.1f}h no-open window "
                              f"because opening here is gap trading")

    if settings.news_enabled and not calendar_usable and settings.news_require_calendar:
        return Verdict(False, "economic calendar unavailable and the news guard is "
                              "set to refuse entries without it")
    for start, end in news_windows:
        if start <= server_time <= end:
            return Verdict(False, f"inside the news blackout {start:%Y-%m-%d %H:%M}"
                                  f"–{end:%H:%M} server time")
    return ALLOWED


def entry_price_acceptable(settings: Settings, direction: int, plan_entry: float,
                           plan_risk: float, live_price: float) -> Verdict:
    """Reject a market entry the market has already run away from.

    The backtest fills at the signal bar's close. If price gapped past that by a
    meaningful slice of R before the order goes in, the live trade is no longer
    the trade that was measured, and its reward-to-risk is worse than modelled.
    """
    adverse = (live_price - plan_entry) * direction
    if plan_risk <= 0:
        return Verdict(False, "plan has no risk distance")
    slip_r = adverse / plan_risk
    if slip_r > settings.max_entry_slippage_r:
        return Verdict(False, f"price already {slip_r:.2f}R past the plan entry "
                              f"(limit {settings.max_entry_slippage_r:.2f}R)")
    return ALLOWED


def progress(settings: Settings, state: BotState, equity: float) -> dict:
    """Where the account stands against each objective, for logging."""
    initial = state.initial_balance or equity
    gain = (equity - initial) / initial * 100 if initial else 0.0
    day_reference = max(state.day_start_balance, state.day_start_equity)
    day_change = ((equity - day_reference) / day_reference * 100
                  if day_reference else 0.0)
    traded_days = len(state.trading_days)
    return {
        "equity": round(equity, 2),
        "target_percent": settings.profit_target_percent,
        "gain_percent": round(gain, 2),
        "target_progress": round(gain / settings.profit_target_percent * 100, 1),
        "day_change_percent": round(day_change, 2),
        "daily_room_percent": round(settings.daily_loss_percent + day_change, 2),
        "max_loss_room_percent": round(settings.max_loss_percent + gain, 2),
        "consecutive_losses": state.consecutive_losses,
        "trading_days": f"{traded_days} of {settings.min_trading_days} required",
        "objectives_met": (gain >= settings.profit_target_percent
                           and traded_days >= settings.min_trading_days),
    }
