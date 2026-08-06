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

    A floor is not meaningful until the durable initial balance is anchored.
    `fallback_balance` is retained only for API compatibility and is never
    allowed to become a live trailing anchor. Live code must call
    `state.anchor_initial_balance()` before any limit is evaluated;
    `anchored()` below refuses to trade until it has.
    """
    initial = float(state.initial_balance or 0.0)
    if initial <= 0:
        return 0.0
    if not settings.trailing_max_loss:
        return initial - settings.max_loss_limit_dollars

    # The anchor only ever rises: the recorded end-of-day high, never a lower
    # current balance.
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


def anchored(state: BotState) -> Verdict:
    """Refuse to trade until the starting balance is on disk.

    Every limit is measured from it. Trading while it is unknown means trading
    against a floor derived from whatever the balance happens to be, which is
    the defect this guard exists to make impossible.
    """
    if float(state.initial_balance or 0.0) > 0:
        return ALLOWED
    return Verdict(False, "initial balance not anchored yet; call "
                          "state.anchor_initial_balance(balance) before trading")


def remaining_room(settings: Settings, state: BotState, equity: float,
                   fallback_balance: float = 0.0,
                   include_reserve: bool = True) -> float:
    """Dollars this account may still risk before the tightest floor is hit.

    The binding constraint is whichever floor is nearest, not the firm's
    headline max loss: a $2,000 max loss with $100 left above the trailing
    floor allows a $100 trade, not a $200 one.

    `loss_room_reserve_dollars` is then held back on top of that. Spending the
    room down to its last dollar leaves the account exactly ON the floor if the
    stop is hit, which is a breach rather than a near miss -- and the daily
    model behind those numbers cannot see a gap through the stop at all. The
    reserve is the difference between "almost never breaches" and "does not
    breach by taking a trade".

    Pass `include_reserve=False` for display, where the operator wants the raw
    distance to the floor rather than the tradeable part of it.
    """
    if not anchored(state):
        return 0.0
    # The reserve guards the floor that ENDS the account, and only that one.
    # Applying it to the daily floors as well was a real cost for no safety:
    # hitting a daily limit at TopStep locks the account out for the day, it
    # does not breach anything, so nothing needs to be held back from it. The
    # daily stop is already $400 and the reserve was $200, which capped every
    # trade at $200 -- the ladder's $500, $375 and $250 tiers could never be
    # reached, and the evaluation took twice as long as it needed to for
    # protection it was already getting elsewhere.
    fatal = max_loss_floor(settings, state, fallback_balance)
    room = float(equity) - fatal
    if include_reserve:
        room -= settings.loss_room_reserve_dollars
    for floor in (daily_loss_floor(settings, state),
                  internal_daily_floor(settings, state)):
        if floor:
            room = min(room, float(equity) - floor)
    return room


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
    weekday, clock = local.weekday(), local.time()
    reopen = time(settings.session_break_end_hour, 0)
    flat_by = time(settings.flat_by_hour, settings.flat_by_minute)

    if weekday == 5:                              # Saturday: no session at all
        return Verdict(False, "exchange closed (Saturday)")
    # The week opens Sunday at the same hour the daily halt ends, 17:00 CT.
    # Checking only for Saturday let Sunday morning through as a normal session,
    # eight hours before the exchange is open.
    if weekday == 6:
        if clock < reopen:
            return Verdict(False, f"exchange closed until Sunday "
                                  f"{reopen:%H:%M} exchange time")
        return ALLOWED
    # Friday's flat deadline ends the week: there is no evening session to
    # reopen into, so the gap runs to Sunday rather than to 17:00.
    if weekday == 4 and clock >= flat_by:
        return Verdict(False, f"week closed at {flat_by:%H:%M} Friday exchange time")
    # Every other weekday: flat by 15:10, dark until the 17:00 reopen, then the
    # evening session is a normal session. Blocking everything after 15:10 --
    # which is what a naive "past the deadline" test does -- would throw away
    # the entire overnight session the strategy trades on M15 and M30.
    if flat_by <= clock < reopen:
        return Verdict(False, f"flat-by window {flat_by:%H:%M}-{reopen:%H:%M} "
                              f"exchange time")
    return ALLOWED


def account_health(settings: Settings, state: BotState, equity: float,
                   balance: float) -> Verdict:
    """Firm limits first, then the tighter internal stops."""
    if state.halted_forever:
        return Verdict(False, f"halted: {state.halted_reason}", fatal=True)

    anchor = anchored(state)
    if not anchor:
        return anchor

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
             active_setups: int | None = None,
             equity: float | None = None,
             open_contracts: int = 0,
             proposed_contracts: int = 0) -> Verdict:
    """Exposure checks for one more entry, in dollars of risk and in contracts.

    `equity` is required because the nearest loss floor is state-dependent. An
    omitted balance used to let an account sitting $100 above its trailing
    floor accept a $200 trade -- and that trade's own stop would end the
    account. There is no safe live/read-only ambiguity at this entry boundary.
    """
    if KILL_SWITCH.exists():
        return Verdict(False, f"kill switch present: {KILL_SWITCH.name}")
    anchor = anchored(state)
    if not anchor:
        return anchor
    if equity is None:
        return Verdict(False, "equity is required to evaluate remaining loss room")
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

    if proposed_contracts:
        verdict = can_hold_contracts(settings, proposed_contracts,
                                     already_held=open_contracts)
        if not verdict:
            return verdict

    if equity is not None:
        room = remaining_room(settings, state, float(equity))
        if room <= 0:
            raw = remaining_room(settings, state, float(equity),
                                 include_reserve=False)
            return Verdict(False, f"no room left: ${raw:,.0f} above the nearest "
                                  f"loss floor, all of it inside the "
                                  f"${settings.loss_room_reserve_dollars:,.0f} reserve")
        # Everything already exposed can still lose its stop distance, so the
        # room has to cover the whole book rather than only the new trade.
        committed = float(open_risk_dollars) + incoming
        if committed > room:
            return Verdict(False, f"${committed:,.0f} of stop risk (open "
                                  f"${open_risk_dollars:,.0f} + new ${incoming:,.0f}) "
                                  f"exceeds the ${room:,.0f} left above the nearest "
                                  f"loss floor")
    return ALLOWED


def contract_cap(settings: Settings) -> int:
    """The firm\'s simultaneous-position cap, in the contract actually traded.

    TopStep publishes one limit in two units: 5 minis OR 50 micros. Configuring
    the mini number while trading a micro understates the cap tenfold, and
    configuring the micro number without aggregate accounting lets two 50-lot
    setups through as 100. Both halves matter, so the cap is derived from the
    contract class here and enforced across the whole book below.
    """
    return (settings.max_micro_contracts if settings.is_micro
            else settings.max_mini_contracts)


def can_hold_contracts(settings: Settings, contracts: int,
                       already_held: int = 0) -> Verdict:
    """Scaling-plan check across every open, pending and proposed contract."""
    if contracts < settings.min_contracts:
        return Verdict(False, f"{contracts} contracts is below the {settings.min_contracts} "
                              f"minimum; risk does not fit a whole contract")
    cap = contract_cap(settings)
    total = int(already_held) + int(contracts)
    if total > cap:
        held = f"{already_held} already held + " if already_held else ""
        return Verdict(False, f"{held}{contracts} contracts exceeds the "
                              f"{cap}-contract simultaneous cap "
                              f"({'micro' if settings.is_micro else 'mini'})")
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


def required_target(settings: Settings, best_day_profit: float) -> float:
    """The profit target after the consistency rule has had its say.

    Breaking consistency does not fail the Combine, it RAISES the bar: a best
    day over 50% of the target moves the target to best / 0.50. Reporting the
    headline $3,000 while the rule has already moved it to $4,000 tells the
    operator to stop and submit an evaluation that has not passed.
    """
    best = max(0.0, float(best_day_profit or 0.0))
    if best <= 0:
        return settings.profit_target_dollars
    return max(settings.profit_target_dollars,
               best / settings.consistency_max_day_share)


def progress(settings: Settings, state: BotState, equity: float) -> dict:
    """Where the evaluation stands: profit against target, and days traded."""
    initial = float(state.initial_balance or settings.initial_balance or 0.0)
    gain = equity - initial if initial > 0 else 0.0
    days = len(state.trading_days)
    best_day = float(getattr(state, "best_day_profit", 0.0) or 0.0)
    target = required_target(settings, best_day)
    target_met = initial > 0 and gain >= target
    days_met = days >= settings.min_trading_days
    return {
        "initial_balance": initial,
        "gain_dollars": gain,
        "target_dollars": target,
        "base_target_dollars": settings.profit_target_dollars,
        "best_day_profit": best_day,
        "target_raised_by_consistency": target > settings.profit_target_dollars,
        "trading_days": days,
        "min_trading_days": settings.min_trading_days,
        "target_met": target_met,
        "days_met": days_met,
        "objectives_met": target_met and days_met,
    }
