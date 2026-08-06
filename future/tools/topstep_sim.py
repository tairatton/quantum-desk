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
XAUUSD spot to MGC futures untested -- see test/future/docs/.
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


def apply_daily_stops(days: np.ndarray, internal_stop: float) -> np.ndarray:
    """Floor each day's loss at whichever stop bites first.

    The bot stands down at its own -$400 and TopStep locks the account at
    -$1,000, so no simulated day may lose more than the tighter of the two.
    This is generous to the bot in one specific way: a single trade that gaps
    through both is not modelled, because the daily series has no intraday path.
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
    drawdown = (np.maximum.accumulate(
        np.concatenate([np.full((len(balance), 1), ACCOUNT_SIZE), balance], axis=1),
        axis=1)[:, 1:] - balance).max(1)
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
        "worst_day": float(np.quantile(days.min(1), .05)),
        "best_day_med": float(np.median(days.max(1))),
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
    print(f"{'scenario':12s}{'risk':>7s}{'PASS':>8s}{'FAIL':>8s}{'open':>7s}"
          f"{'days':>12s}{'dd med':>8s}{'dd p95':>8s}{'worst day':>11s}"
          f"{'best day':>10s}")
    for scenario in scenarios:
        for risk in risks:
            m = simulate(scenario, risk, corr, require_winning_days, internal_stop)
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
    parser.add_argument("--require-winning-days", type=int, default=0,
                        help="sources disagree on whether the Combine still "
                             "needs winning days; 0 = target only")
    parser.add_argument("--scenario", action="append",
                        choices=("holdout", "validation", "train"))
    args = parser.parse_args()
    if args.nsim < 100:
        parser.error("--nsim must be at least 100")
    NSIM = args.nsim
    report(args.risk or (100.0, 150.0, 200.0, 300.0),
           args.scenario or ("holdout", "validation", "train"),
           args.corr, args.require_winning_days, args.internal_stop)


if __name__ == "__main__":
    main()
