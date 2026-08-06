"""The rules that decide whether a futures trade is allowed at all.

Same shape as the forex guardrails and deliberately not the same code. Three
differences carry real money:

1.  TopStep's max loss TRAILS the highest end-of-day balance. FTMO's is fixed at
    10% below the initial balance. A trailing floor can be breached by a day
    that only gave back what an earlier day won, which a static floor cannot.
2.  Limits are dollars, not percentages of a moving balance.
3.  The account must be flat before the daily close. There is no equivalent
    rule at FTMO, and being late is a breach rather than a bad fill.

A guard never sizes down to fit -- it refuses.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from engine.state import BotState

from .settings import KILL_SWITCH, Settings


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str
    fatal: bool = False    # True = stop the process, not just this trade

    def __bool__(self) -> bool:
        return self.allowed


ALLOWED = Verdict(True, "ok")


def max_loss_floor(settings: Settings, state: BotState,
                   fallback_balance: float = 0.0) -> float:
    """The equity level that ends the account.

    Trailing: highest end-of-day balance minus the max loss limit, capped at the
    starting balance once the account is funded and the rule stops trailing.
    Static: initial balance minus the limit, which is what a Combine that does
    not trail, or FTMO, would use.
    """
    initial = float(state.initial_balance or settings.initial_balance
                    or fallback_balance or 0.0)
    if initial <= 0:
        return 0.0
    if not settings.trailing_max_loss:
        return initial - settings.max_loss_limit_dollars

    anchor = max(float(state.eod_balance_high_water or 0.0), initial)
    floor = anchor - settings.max_loss_limit_dollars
    if settings.trailing_stops_at_initial_balance:
        # Once the trailing floor reaches the starting balance it freezes there;
        # letting it keep climbing would invent a rule the firm does not have.
        floor = min(floor, initial)
    return floor


def daily_loss_floor(settings: Settings, state: BotState) -> float:
    """Firm daily limit, measured from the balance the trading day opened on."""
    reference = float(state.day_start_balance or 0.0)
    if reference <= 0:
        return 0.0
    return reference - settings.daily_loss_limit_dollars


def internal_daily_floor(settings: Settings, state: BotState) -> float:
    """The bot's own, tighter daily stop."""
    reference = float(state.day_start_balance or 0.0)
    if reference <= 0:
        return 0.0
    return reference - settings.internal_daily_stop_dollars


def flat_deadline(settings: Settings, now_exchange: datetime) -> datetime:
    """The moment every position must already be closed, on the exchange clock."""
    return now_exchange.replace(hour=settings.flat_by_hour,
                                minute=settings.flat_by_minute,
                                second=0, microsecond=0)


def exchange_now(settings: Settings, moment: datetime) -> datetime:
    """`moment` on the exchange clock. Naive input is taken to be exchange time."""
    zone = ZoneInfo(settings.exchange_timezone)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=zone)
    return moment.astimezone(zone)


def session_open(settings: Settings, moment: datetime) -> Verdict:
    """Refuse entries inside the maintenance halt and after the flat deadline.

    The bot stands down before the deadline rather than at it: an entry taken
    one minute before the close would have to be closed one minute later, which
    is not the system the lab measured, and a fill that slips past the deadline
    is a rule breach.
    """
    local = exchange_now(settings, moment)
    if local.weekday() == 5:                      # Saturday: no session at all
        return Verdict(False, "exchange closed (Saturday)")
    break_start = time(settings.session_break_start_hour, 0)
    break_end = time(settings.session_break_end_hour, 0)
    if break_start <= local.time() < break_end:
        return Verdict(False, f"daily maintenance halt "
                              f"{break_start:%H:%M}-{break_end:%H:%M} exchange time")
    if local >= flat_deadline(settings, local):
        return Verdict(False, f"past the flat-by deadline "
                              f"{settings.flat_by_hour:02d}:{settings.flat_by_minute:02d} "
                              f"exchange time")
    return ALLOWED


def account_health(settings: Settings, state: BotState, equity: float,
                   balance: float) -> Verdict:
    """Firm limits first, then the tighter internal stops."""
    if state.halted_forever:
        return Verdict(False, f"halted: {state.halted_reason}", fatal=True)

    hard_floor = max_loss_floor(settings, state, balance)
    if hard_floor and equity <= hard_floor:
        state.halt(f"max loss breached: equity {equity:,.2f} <= {hard_floor:,.2f}")
        return Verdict(False, state.halted_reason, fatal=True)

    daily_floor = daily_loss_floor(settings, state)
    if daily_floor and equity <= daily_floor:
        state.pause_today()
        return Verdict(False, f"daily loss limit hit: equity {equity:,.2f} "
                              f"<= {daily_floor:,.2f}")

    internal_floor = internal_daily_floor(settings, state)
    if internal_floor and equity <= internal_floor:
        state.pause_today()
        return Verdict(False, f"internal daily stop ${settings.internal_daily_stop_dollars:,.0f}"
                              f" hit at equity {equity:,.2f}")

    if settings.stop_at_target:
        standing = progress(settings, state, equity)
        if standing["objectives_met"]:
            return Verdict(False, f"objectives met: {standing['gain_dollars']:+,.2f} over "
                                  f"{standing['trading_days']} trading days — stop and "
                                  f"submit. Set stop_at_target false to carry on.")

    if state.consecutive_losses >= settings.max_consecutive_losses:
        state.pause_today()
        return Verdict(False, f"{state.consecutive_losses} losses in a row; "
                              f"standing down until the next trading day")
    if state.is_paused_today:
        return Verdict(False, "paused for the rest of this trading day")
    return ALLOWED


def can_open(settings: Settings, state: BotState, open_risk_dollars: float,
             open_count: int, pending_count: int,
             proposed_risk_dollars: float | None = None,
             active_setups: int | None = None) -> Verdict:
    """Exposure checks for one more entry, in dollars of risk."""
    if KILL_SWITCH.exists():
        return Verdict(False, f"kill switch present: {KILL_SWITCH.name}")
    exposure_count = (open_count + pending_count
                      if active_setups is None else active_setups)
    if exposure_count >= settings.max_concurrent_trades:
        return Verdict(False, f"{exposure_count} setups already live; "
                              f"max_concurrent_trades={settings.max_concurrent_trades}")
    incoming = (settings.risk_dollars if proposed_risk_dollars is None
                else float(proposed_risk_dollars))
    if open_risk_dollars + incoming > settings.max_open_risk_dollars:
        return Verdict(False, f"open risk ${open_risk_dollars:,.0f} + ${incoming:,.0f} "
                              f"exceeds ${settings.max_open_risk_dollars:,.0f}")
    return ALLOWED


def can_hold_contracts(settings: Settings, contracts: int) -> Verdict:
    """Scaling-plan check. Exceeding the contract cap is a rule breach itself."""
    if contracts < settings.min_contracts:
        return Verdict(False, f"{contracts} contracts is below the {settings.min_contracts} "
                              f"minimum; risk does not fit a whole contract")
    if contracts > settings.max_contracts:
        return Verdict(False, f"{contracts} contracts exceeds the scaling plan cap of "
                              f"{settings.max_contracts}")
    return ALLOWED


def consistency(settings: Settings, best_day_profit: float,
                total_profit: float) -> Verdict:
    """Payout consistency check.

    This one gates a withdrawal rather than the account, so a failure is a
    warning the operator has to act on -- stopping trading over it would cost
    more than it saves.
    """
    if total_profit <= 0 or best_day_profit <= 0:
        return ALLOWED
    share = best_day_profit / total_profit
    if share > settings.consistency_max_day_share:
        return Verdict(False, f"best day is {share:.0%} of total profit, over the "
                              f"{settings.consistency_max_day_share:.0%} consistency "
                              f"limit; keep trading to dilute it before requesting "
                              f"a payout")
    return ALLOWED


def progress(settings: Settings, state: BotState, equity: float) -> dict:
    """Where the evaluation stands: profit against target, and days traded."""
    initial = float(state.initial_balance or settings.initial_balance or 0.0)
    gain = equity - initial if initial > 0 else 0.0
    days = len(state.trading_days)
    target_met = initial > 0 and gain >= settings.profit_target_dollars
    days_met = days >= settings.min_trading_days
    return {
        "initial_balance": initial,
        "gain_dollars": gain,
        "target_dollars": settings.profit_target_dollars,
        "trading_days": days,
        "min_trading_days": settings.min_trading_days,
        "target_met": target_met,
        "days_met": days_met,
        "objectives_met": target_met and days_met,
    }
