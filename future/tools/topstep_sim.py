"""Does this system pass a TopStep Combine? Simulated on the same gold edge.

The FTMO simulator cannot answer this, and not because the numbers differ. The
rules differ in kind:

    FTMO 2-Step                        TopStep Combine
    max loss 10% of INITIAL, static    max loss $2,000 below the highest
                                       END-OF-DAY balance, ratcheting up
    daily loss 5% -> breach, over      daily loss $1,000 -> locked out for the
                                       day, come back tomorrow
    target 10% then 5%                 target $3,000, once
    4 minimum trading days             consistency: best day <= 50% of target,
                                       or the target rises to best/0.5

A ratcheting floor is the whole story. Under FTMO a good week buys permanent
room, because the floor never moves. Under TopStep a good week MOVES THE FLOOR
UP behind you, so giving back what you just made can end an account that is
still in profit -- and the floor only stops trailing once end-of-day balance
reaches $52,000, from where it freezes at the $50,000 starting balance.

    python tools/topstep_sim.py
    python tools/topstep_sim.py --risk 150 --nsim 20000
    python tools/topstep_sim.py --require-winning-days 5

The trade series is the same measured gold edge the forex book trades, resampled
the same way, so this asks "given this edge, what do TopStep's rules do to it".
It cannot validate the edge, and it does not claim the edge transfers from
XAUUSD spot to MGC futures untested -- see test/docs/.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy import backtest_reporting, config  # noqa: E402
from bot.settings import Settings as FuturesSettings  # noqa: E402
from engine.dynamic_risk import ladder_steps  # noqa: E402

NSIM, MAXDAYS = 20_000, 400
STREAMS = (("XAUUSD", "M15"), ("XAUUSD", "M30"))
TECHNIQUE = "be_after_tp1_33_33_34"       # the production exit at both venues

# --- TopStep 50K Combine, as published for 2026 ----------------------------
# Every figure here is a rule, not a preference. Confirm against TopStep's own
# rulebook before trusting a result: they change, and a wrong figure makes this
# script confidently wrong.
ACCOUNT_SIZE = 50_000.0
PROFIT_TARGET = 3_000.0
MAX_LOSS_LIMIT = 2_000.0                  # trailing, on end-of-day balance
DAILY_LOSS_LIMIT = 1_000.0                # lockout for the day, not a breach
TRAIL_FREEZE_AT = ACCOUNT_SIZE + MAX_LOSS_LIMIT   # $52,000 -> floor freezes
CONSISTENCY_SHARE = 0.50                  # best day <= 50% of the target
WINNING_DAY = 150.0                       # a day counts as a win at +$150 net

# --- the bot's own, tighter stop -------------------------------------------
INTERNAL_DAILY_STOP = 400.0
# Held back above the nearest floor and never risked; see
# `bot.settings.loss_room_reserve_dollars`.
LOSS_ROOM_RESERVE = 200.0

# The drawdown ladder, in dollars: the same tiers the forex instance runs as
# 1.00 / 0.75 / 0.50 / 0.40% of a 50,000 account, stepping down as the account
# draws down from its closed high-water mark. `--ladder` prices the technique as
# the bot would actually run it; a flat `--risk` prices a single tier.
# (drawdown threshold, risk). The last four steps continue the same $250 spacing
# past the floor tier, so an account close to its trailing floor keeps shrinking
# the trade instead of holding it flat exactly where the floor is nearest.
# Keep the simulator on the exact production ladder.  Duplicating these
# thresholds in a tool let the engine rebound to $200 at a $1,500 drawdown
# while the report stayed at the $50 recovery tier.
LADDER = ladder_steps(FuturesSettings())
RECOVERY_TIERS = (100.0, 50.0)


def daily_r(symbol: str, timeframe: str) -> np.ndarray:
    """Daily R totals over the stream's holdout span, including flat days."""
    report = backtest_reporting.load_report(
        backtest_reporting.report_path(symbol, timeframe))
    curve = report["holdout_all_curves_r"]
    equity = np.asarray(curve["equity"][TECHNIQUE], dtype=float)
    trade_r = np.diff(np.concatenate([[0.0], equity]))
    stamps = pd.to_datetime(curve["time"])
    series = pd.Series(trade_r, index=stamps)
    by_day = series.groupby(series.index.date).sum()
    span = pd.date_range(stamps.min().normalize(), stamps.max().normalize(), freq="D")
    traded = set(by_day.index)
    span = pd.DatetimeIndex([d for d in span if d.dayofweek < 5 or d.date() in traded])
    days = pd.Series(0.0, index=span.date)
    days.loc[list(by_day.index)] = by_day.values
    return days.to_numpy()


def pools(scenario: str) -> list[np.ndarray]:
    """One pool of daily R per stream, re-based to the scenario's expectancy."""
    out = []
    for symbol, timeframe in STREAMS:
        report = backtest_reporting.load_report(
            backtest_reporting.report_path(symbol, timeframe))
        metrics = report["techniques"][TECHNIQUE]
        days = daily_r(symbol, timeframe)
        if scenario != "holdout":
            # Shift the daily mean to the split's expectancy, keeping the shape.
            trades_per_day = len(report["holdout_all_curves_r"]["time"]) / len(days)
            target = metrics[scenario]["expectancy_r"] * trades_per_day
            days = days - days.mean() + target
        out.append(days)
    return out


def daily_dollars(scenario: str, risk_dollars: float, corr: float,
                  rng: np.random.Generator) -> np.ndarray:
    """(NSIM, MAXDAYS) of portfolio daily P&L in dollars, before any rule."""
    shape = (NSIM, MAXDAYS)
    streams = pools(scenario)
    if corr <= 0:
        total = sum(rng.choice(pool, size=shape) for pool in streams)
    else:
        quantile = rng.random(shape)
        total = sum((1 - corr) * rng.choice(pool, size=shape)
                    + corr * np.quantile(pool, quantile) for pool in streams)
    return total * risk_dollars


def ladder_risk(drawdown: np.ndarray) -> np.ndarray:
    """Risk in dollars for each path, from its current drawdown."""
    risk = np.full(drawdown.shape, LADDER[-1][1])
    for threshold, size in reversed(LADDER[:-1]):
        risk = np.where(drawdown < threshold, size, risk)
    return risk


def simulate_ladder(scenario: str, corr: float, internal_stop: float,
                    require_winning_days: int = 0, seed: int = 41) -> dict:
    """Walk the account day by day so the ladder can respond to drawdown.

    The flat-risk simulation multiplies a whole matrix at once, which cannot
    express a size that depends on where the account already is. This one pays
    for the loop: each day is sized from the previous day's drawdown against the
    closed high-water mark, exactly as `engine.dynamic_risk.decide_dollars` does.
    """
    rng = np.random.default_rng(seed)
    streams = pools(scenario)
    shape = (NSIM, MAXDAYS)
    quantile = rng.random(shape)
    unit = sum((1 - corr) * rng.choice(pool, size=shape)
               + corr * np.quantile(pool, quantile) for pool in streams)

    balance = np.full(NSIM, ACCOUNT_SIZE)
    high_water = np.full(NSIM, ACCOUNT_SIZE)
    floor = np.full(NSIM, ACCOUNT_SIZE - MAX_LOSS_LIMIT)
    best_day = np.zeros(NSIM)
    worst_day = np.zeros(NSIM)
    winning_days = np.zeros(NSIM, dtype=int)
    alive = np.ones(NSIM, dtype=bool)
    passed = np.zeros(NSIM, dtype=bool)
    failed = np.zeros(NSIM, dtype=bool)
    pass_day = np.full(NSIM, MAXDAYS + 10)
    peak_balance = balance.copy()
    max_dd = np.zeros(NSIM)

    for day in range(MAXDAYS):
        risk = ladder_risk(np.maximum(0.0, high_water - balance))
        pnl = np.maximum(unit[:, day] * risk, -min(internal_stop, DAILY_LOSS_LIMIT))
        balance = np.where(alive, balance + pnl, balance)
        best_day = np.where(alive, np.maximum(best_day, pnl), best_day)
        worst_day = np.where(alive, np.minimum(worst_day, pnl), worst_day)
        winning_days += (alive & (pnl >= WINNING_DAY)).astype(int)
        peak_balance = np.maximum(peak_balance, balance)
        max_dd = np.maximum(max_dd, peak_balance - balance)

        breached = alive & (balance <= floor)
        failed |= breached
        alive &= ~breached

        required = np.maximum(PROFIT_TARGET, best_day / CONSISTENCY_SHARE)
        won = alive & ((balance - ACCOUNT_SIZE) >= required)
        if require_winning_days:
            # Regression: ladder mode took the flag and dropped it, so even an
            # impossible requirement produced identical output.
            won &= winning_days >= require_winning_days
        pass_day = np.where(won & ~passed, day + 1, pass_day)
        passed |= won
        alive &= ~won

        high_water = np.maximum(high_water, balance)
        floor = np.maximum(floor, np.minimum(balance - MAX_LOSS_LIMIT, ACCOUNT_SIZE))

    pick = lambda values, q: int(np.quantile(values, q)) if len(values) else -1
    return {"scenario": scenario, "risk": -1, "pass": passed.mean(),
            "fail": failed.mean(), "unresolved": (~passed & ~failed).mean(),
            "days_med": pick(pass_day[passed], .5),
            "days_p90": pick(pass_day[passed], .9),
            "dd_med": float(np.median(max_dd)), "dd_p95": float(np.quantile(max_dd, .95)),
            "worst_day": float(np.quantile(worst_day, .05)),
            "best_day_med": float(np.median(best_day))}


def simulate_room_aware(scenario: str, corr: float, internal_stop: float,
                        require_winning_days: int = 0, seed: int = 41,
                        reserve: float = LOSS_ROOM_RESERVE) -> dict:
    """The ladder, plus the guard that refuses a trade bigger than the room left.

    `simulate_ladder` sizes from drawdown alone, which is what the bot did
    before `guardrails.remaining_room` existed. This models what it does now:
    every day the risk is the ladder tier fitted into the distance between
    equity and the nearest floor, and when no configured tier fits, the day is
    simply not traded.

    That single rule is what turns "almost never fails" into "cannot fail by
    taking a trade": a breach then requires the market to gap through a stop,
    not the bot to have knowingly risked more than it had. The cost is real and
    shows up as unresolved paths -- an account that stops trading to stay alive
    does not pass either.

    `reserve` holds back a fixed amount above the floor, for the gap risk this
    daily model cannot see.
    """
    rng = np.random.default_rng(seed)
    streams = pools(scenario)
    shape = (NSIM, MAXDAYS)
    quantile = rng.random(shape)
    unit = sum((1 - corr) * rng.choice(pool, size=shape)
               + corr * np.quantile(pool, quantile) for pool in streams)

    tiers = np.array(sorted({size for _, size in LADDER} | set(RECOVERY_TIERS),
                            reverse=True))
    smallest = float(tiers.min())

    balance = np.full(NSIM, ACCOUNT_SIZE)
    high_water = np.full(NSIM, ACCOUNT_SIZE)
    floor = np.full(NSIM, ACCOUNT_SIZE - MAX_LOSS_LIMIT)
    best_day = np.zeros(NSIM)
    worst_day = np.zeros(NSIM)
    winning_days = np.zeros(NSIM, dtype=int)
    alive = np.ones(NSIM, dtype=bool)
    passed = np.zeros(NSIM, dtype=bool)
    failed = np.zeros(NSIM, dtype=bool)
    stood_down = np.zeros(NSIM, dtype=bool)
    pass_day = np.full(NSIM, MAXDAYS + 10)
    peak_balance = balance.copy()
    max_dd = np.zeros(NSIM)

    for day in range(MAXDAYS):
        drawdown = np.maximum(0.0, high_water - balance)
        tier = ladder_risk(drawdown)
        # The reserve guards the account-ending floor only; the daily stop is a
        # lockout and needs nothing held back from it.
        room = np.minimum(balance - floor - reserve, internal_stop)
        # Fit the tier into the room, exactly as `dynamic_risk.fit_to_room`
        # does: the largest configured tier that fits, or nothing.
        risk = np.zeros(NSIM)
        for size in tiers:
            # The ladder tier is the ceiling; the room decides what fits under it.
            risk = np.where((risk == 0) & (size <= np.minimum(room, tier)), size, risk)
        traded = alive & (room >= smallest) & (risk > 0)
        stood_down |= alive & ~traded

        pnl = np.where(traded,
                       np.maximum(unit[:, day] * risk, -np.minimum(internal_stop, room)),
                       0.0)
        balance = balance + pnl
        best_day = np.where(alive, np.maximum(best_day, pnl), best_day)
        worst_day = np.where(alive, np.minimum(worst_day, pnl), worst_day)
        winning_days += (alive & (pnl >= WINNING_DAY)).astype(int)
        peak_balance = np.maximum(peak_balance, balance)
        max_dd = np.maximum(max_dd, peak_balance - balance)

        breached = alive & (balance <= floor)
        failed |= breached
        alive &= ~breached

        required = np.maximum(PROFIT_TARGET, best_day / CONSISTENCY_SHARE)
        won = alive & ((balance - ACCOUNT_SIZE) >= required)
        if require_winning_days:
            won &= winning_days >= require_winning_days
        pass_day = np.where(won & ~passed, day + 1, pass_day)
        passed |= won
        alive &= ~won

        high_water = np.maximum(high_water, balance)
        floor = np.maximum(floor, np.minimum(balance - MAX_LOSS_LIMIT, ACCOUNT_SIZE))

    pick = lambda values, q: int(np.quantile(values, q)) if len(values) else -1
    return {"scenario": scenario, "risk": -2, "pass": passed.mean(),
            "fail": failed.mean(), "unresolved": (~passed & ~failed).mean(),
            "days_med": pick(pass_day[passed], .5),
            "days_p90": pick(pass_day[passed], .9),
            "dd_med": float(np.median(max_dd)), "dd_p95": float(np.quantile(max_dd, .95)),
            "worst_day": float(np.quantile(worst_day, .05)),
            "best_day_med": float(np.median(best_day)),
            "stood_down": float(stood_down.mean())}


def apply_daily_stops(days: np.ndarray, internal_stop: float) -> np.ndarray:
    """Clip each day's NET result at the tighter of the two daily stops.

    APPROXIMATION, and the reports say so. The lockout is a rule about the
    ORDER of trades inside a day: the live bot stops the moment cumulative loss
    reaches -$400 and never takes the rest of that day's trades. This function
    only sees the day's net total, so a day that went -$500 and then +$800 is
    seen as +$300 and left untouched, when the live bot would have locked out
    at -$400 and never taken the recovery trade.

    The source reports keep per-trade R with timestamps, so an ordered intraday
    truncation is possible in principle -- but `pools()` has already summed the
    day by the time anything gets here, and rebuilding the trade sequence is a
    change to the data path rather than to this function. Until that is done,
    results are labelled "daily-net approximation" wherever they are printed,
    and they are OPTIMISTIC: they credit recoveries the bot would not have made
    and they understate how often the account is locked out.
    """
    return np.maximum(days, -min(internal_stop, DAILY_LOSS_LIMIT))


def simulate(scenario: str, risk_dollars: float, corr: float,
             require_winning_days: int, internal_stop: float,
             seed: int = 41) -> dict:
    """Run the Combine to a verdict on every path."""
    rng = np.random.default_rng(seed)
    days = apply_daily_stops(daily_dollars(scenario, risk_dollars, corr, rng),
                             internal_stop)

    balance = ACCOUNT_SIZE + np.cumsum(days, axis=1)
    running_peak = np.maximum.accumulate(balance, axis=1)

    # The floor ratchets on END-OF-DAY balance and freezes once the account has
    # closed a day at $52,000. `np.minimum` is the freeze: past that point the
    # floor is the starting balance and stops following.
    floor = np.minimum(running_peak - MAX_LOSS_LIMIT, ACCOUNT_SIZE)
    floor = np.maximum(floor, ACCOUNT_SIZE - MAX_LOSS_LIMIT)
    # Yesterday's floor governs today: the ratchet uses the previous close.
    floor = np.concatenate([np.full((len(floor), 1), ACCOUNT_SIZE - MAX_LOSS_LIMIT),
                            floor[:, :-1]], axis=1)

    never = MAXDAYS + 10
    first = lambda mask: np.where(mask.any(1), mask.argmax(1), never)

    breach_day = first(balance <= floor)

    # Consistency: a best day over 50% of the target does not fail the account,
    # it raises the target to best/0.5. So the target each path must reach is
    # path dependent, and a single outsized day makes the finish line recede.
    best_day = np.maximum.accumulate(days, axis=1)
    # `days` here is already stop-clipped; `live` above masks post-resolution.
    required = np.maximum(PROFIT_TARGET, best_day / CONSISTENCY_SHARE)
    profit = balance - ACCOUNT_SIZE
    target_day = first(profit >= required)

    if require_winning_days:
        wins = np.cumsum(days >= WINNING_DAY, axis=1)
        wins_day = first(wins >= require_winning_days)
        pass_day = np.maximum(target_day, wins_day)
        pass_day = np.where((target_day >= never) | (wins_day >= never), never, pass_day)
    else:
        pass_day = target_day

    passed = pass_day < breach_day
    failed = breach_day < pass_day
    unresolved = ~passed & ~failed

    pick = lambda values, q: int(np.quantile(values, q)) if len(values) else -1

    # Freeze each path at its first terminal event before measuring anything.
    # Without this the drawdown, best day and worst day were computed over all
    # 400 generated days INCLUDING days after the account had already passed or
    # blown up -- days that never happen. Ladder mode stops resolved paths, so
    # the two modes disagreed even when the ladder was pinned to one tier.
    resolved = np.minimum(np.minimum(pass_day, breach_day), MAXDAYS - 1)
    live = np.arange(MAXDAYS)[None, :] <= resolved[:, None]
    days_live = np.where(live, days, 0.0)
    balance_live = ACCOUNT_SIZE + np.cumsum(days_live, axis=1)
    drawdown = (np.maximum.accumulate(
        np.concatenate([np.full((len(balance_live), 1), ACCOUNT_SIZE), balance_live],
                       axis=1), axis=1)[:, 1:] - balance_live).max(1)
    return {
        "scenario": scenario,
        "risk": risk_dollars,
        "pass": passed.mean(),
        "fail": failed.mean(),
        "unresolved": unresolved.mean(),
        "days_med": pick(pass_day[passed] + 1, .5),
        "days_p90": pick(pass_day[passed] + 1, .9),
        "dd_med": float(np.median(drawdown)),
        "dd_p95": float(np.quantile(drawdown, .95)),
        "worst_day": float(np.quantile(np.where(live, days, 0.0).min(1), .05)),
        # Masked like every other reported statistic: a best day recorded after
        # the account passed or blew up is a day that never happened.
        "best_day_med": float(np.median(np.where(live, days, 0.0).max(1))),
        "consistency_bit": float((best_day[:, -1] > PROFIT_TARGET
                                  * CONSISTENCY_SHARE).mean()),
    }


def report(risks, scenarios, corr: float, require_winning_days: int,
           internal_stop: float) -> None:
    print(f"\nTopStep {ACCOUNT_SIZE:,.0f} Combine | target ${PROFIT_TARGET:,.0f} | "
          f"MLL ${MAX_LOSS_LIMIT:,.0f} trailing end-of-day, freezing at "
          f"${TRAIL_FREEZE_AT:,.0f} | DLL ${DAILY_LOSS_LIMIT:,.0f} | "
          f"internal stop ${internal_stop:,.0f} | consistency "
          f"{CONSISTENCY_SHARE:.0%} | corr={corr}"
          + (f" | {require_winning_days} winning days required" if require_winning_days
             else " | no winning-day requirement"))
    print("  daily-net approximation: the lockout clips a day\'s NET result, not "
          "its trade sequence, so recoveries after an intraday stop are credited "
          "and results are optimistic.")
    print(f"{'scenario':12s}{'risk':>7s}{'PASS':>8s}{'FAIL':>8s}{'open':>7s}"
          f"{'days':>12s}{'dd med':>8s}{'dd p95':>8s}{'worst day':>11s}"
          f"{'best day':>10s}")
    for scenario in scenarios:
        for risk in risks:
            if risk == -2:
                m = simulate_room_aware(scenario, corr, internal_stop,
                                        require_winning_days)
            elif risk < 0:
                m = simulate_ladder(scenario, corr, internal_stop,
                                    require_winning_days)
            else:
                m = simulate(scenario, risk, corr, require_winning_days,
                             internal_stop)
            print(f"{m['scenario']:12s}{m['risk']:7.0f}{m['pass']:8.1%}{m['fail']:8.1%}"
                  f"{m['unresolved']:7.1%}"
                  f"{f'{m['days_med']}/{m['days_p90']}':>12s}"
                  f"{m['dd_med']:8.0f}{m['dd_p95']:8.0f}{m['worst_day']:+11.0f}"
                  f"{m['best_day_med']:+10.0f}", flush=True)


def main() -> None:
    global NSIM

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--risk", type=float, action="append",
                        help="risk per trade in dollars (repeatable)")
    parser.add_argument("--corr", type=float, default=0.3)
    parser.add_argument("--nsim", type=int, default=NSIM)
    parser.add_argument("--internal-stop", type=float, default=INTERNAL_DAILY_STOP)
    parser.add_argument("--ladder", action="store_true",
                        help="size with the drawdown ladder instead of flat risk")
    parser.add_argument("--room-aware", action="store_true",
                        help="ladder plus the remaining-room guard, i.e. what the "
                             "bot actually does now")
    parser.add_argument("--require-winning-days", type=int, default=0,
                        help="sources disagree on whether the Combine still "
                             "needs winning days; 0 = target only")
    parser.add_argument("--scenario", action="append",
                        choices=("holdout", "validation", "train"))
    args = parser.parse_args()
    if args.nsim < 100:
        parser.error("--nsim must be at least 100")
    NSIM = args.nsim
    report([-2.0] if args.room_aware else
           [-1.0] if args.ladder else args.risk or (100.0, 150.0, 200.0, 300.0),
           args.scenario or ("holdout", "validation", "train"),
           args.corr, args.require_winning_days, args.internal_stop)


if __name__ == "__main__":
    main()
