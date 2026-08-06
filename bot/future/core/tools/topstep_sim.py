"""Does this system pass a TopStep Combine?

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

    python tools/topstep_sim.py                 # MGC Yahoo + production guard
    python tools/topstep_sim.py --risk 150 --nsim 20000  # fixed-risk experiment
    python tools/topstep_sim.py --flat           # fixed-risk MGC reference table
    python tools/topstep_sim.py --require-winning-days 5

By default the simulator runs the futures strategy over the locally cached
``MGC=F`` Yahoo bars (the free 60-day smoke-test feed). Yahoo is delayed, rolled
and has no broker spread, so the MGC result is a bot/data-path check, not a
long-history probability of passing a real Combine. Forex/XAUUSD data is not a
valid input to this simulator and is intentionally rejected by the data path.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy import config, quantum  # noqa: E402
from bot.broker import OrderRejected  # noqa: E402
from bot.settings import load as load_settings  # noqa: E402
from bot.trader import plan_contracts  # noqa: E402
from engine.dynamic_risk import ladder_steps  # noqa: E402

NSIM, MAXDAYS = 20_000, 400
MGC_STREAMS = (("MGC", "M15"), ("MGC", "M30"))
MGC_SCENARIO = "yahoo_60d"
MGC_DATA_DIR = config.MARKET_DATA_DIR / "MGC"

# --- TopStep 50K Combine, as published for 2026 ----------------------------
# Every figure here is a rule, not a preference. Confirm against TopStep's own
# rulebook before trusting a result: they change, and a wrong figure makes this
# script confidently wrong.
PRODUCTION_SETTINGS = load_settings()
ACCOUNT_SIZE = PRODUCTION_SETTINGS.account_size
PROFIT_TARGET = PRODUCTION_SETTINGS.profit_target_dollars
MAX_LOSS_LIMIT = PRODUCTION_SETTINGS.max_loss_limit_dollars  # EOD trailing
DAILY_LOSS_LIMIT = PRODUCTION_SETTINGS.daily_loss_limit_dollars  # lockout
TRAIL_FREEZE_AT = ACCOUNT_SIZE + MAX_LOSS_LIMIT
CONSISTENCY_SHARE = PRODUCTION_SETTINGS.consistency_max_day_share
WINNING_DAY = 150.0                       # a day counts as a win at +$150 net

# --- the bot's own, tighter stop -------------------------------------------
INTERNAL_DAILY_STOP = PRODUCTION_SETTINGS.internal_daily_stop_dollars
# Held back above the nearest floor and never risked; see
# `bot.settings.loss_room_reserve_dollars`.
LOSS_ROOM_RESERVE = PRODUCTION_SETTINGS.loss_room_reserve_dollars

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
LADDER = ladder_steps(PRODUCTION_SETTINGS)
RECOVERY_TIERS = PRODUCTION_SETTINGS.recovery_risk_dollars
DEFAULT_FIXED_RISKS = (100.0, 150.0, 200.0, 300.0)


def select_risks(risk: list[float] | None = None, *, ladder: bool = False,
                 room_aware: bool = False, flat: bool = False) -> list[float]:
    """Resolve CLI sizing flags, defaulting to the exact production path.

    ``--risk`` and ``--flat`` are deliberately opt-in fixed-risk experiments.
    With no sizing flag the simulator must answer what the live bot does:
    dynamic ladder plus the remaining-room/reserve guard.
    """
    selected = sum((risk is not None, ladder, room_aware, flat))
    if selected > 1:
        raise ValueError("choose only one of --risk, --ladder, --room-aware, or --flat")
    if risk is not None:
        return list(risk)
    if flat:
        return list(DEFAULT_FIXED_RISKS)
    if ladder:
        return [-1.0]
    return [-2.0]


_MGC_ANALYSIS: dict[str, dict] = {}
_MGC_DOLLAR_POOLS: dict[float, list[np.ndarray]] = {}
_MGC_INFO: dict[str, object] | None = None


def _load_mgc_analysis(timeframe: str) -> dict:
    """Load and analyse one Yahoo MGC stream, refusing unlabelled data."""
    global _MGC_INFO
    if timeframe in _MGC_ANALYSIS:
        return _MGC_ANALYSIS[timeframe]
    path = MGC_DATA_DIR / f"{timeframe}.csv"
    meta_path = path.with_suffix(".meta.json")
    if not path.exists() or not meta_path.exists():
        raise RuntimeError(
            f"MGC Yahoo {timeframe} data is missing: {path}. Run "
            "python tools/download_mgc_yahoo.py --period 60d first."
        )
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if (str(meta.get("source", "")).lower() != "yahoo"
            or str(meta.get("symbol", "")) != "MGC=F"
            or str(meta.get("key", "")) != "MGC"):
        raise RuntimeError(
            f"Refusing non-MGC-Yahoo data in {meta_path}; expected "
            "source=yahoo, symbol=MGC=F, key=MGC"
        )
    frame = pd.read_csv(path, parse_dates=["time"])
    required = {"time", "open", "high", "low", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(f"MGC {timeframe} CSV is missing columns: {sorted(missing)}")
    frame = frame.sort_values("time").drop_duplicates("time").reset_index(drop=True)
    result = quantum.analyse(frame, timeframe)
    filled = [p for p in result["plans"] if p["entry_fill_index"] is not None]
    info = {
        "timeframe": timeframe,
        "bars": len(frame),
        "plans": len(result["plans"]),
        "filled": len(filled),
        "first": str(pd.Timestamp(frame["time"].iloc[0])),
        "last": str(pd.Timestamp(frame["time"].iloc[-1])),
    }
    _MGC_ANALYSIS[timeframe] = {"frame": frame, "result": result, "info": info}
    if _MGC_INFO is None:
        _MGC_INFO = {"first": info["first"], "last": info["last"], "streams": []}
    _MGC_INFO["first"] = min(str(_MGC_INFO["first"]), info["first"])
    _MGC_INFO["last"] = max(str(_MGC_INFO["last"]), info["last"])
    _MGC_INFO["streams"].append(info)
    return _MGC_ANALYSIS[timeframe]


def _mgc_trade_pnl(plan: dict, requested_risk: float) -> float | None:
    """Return one filled MGC trade's dollar result at a requested tier.

    Contract rounding and the production exit switch are applied before the
    payoff is converted to dollars.  This is the part a continuous-risk model
    could never represent.
    """
    if any(value is None for value in plan["resolved"]):
        return None
    try:
        sized = plan_contracts(PRODUCTION_SETTINGS, plan["entry"], plan["stop"],
                               requested_risk)
    except OrderRejected:
        return None
    unit_risk = float(sized.stop_distance_points * PRODUCTION_SETTINGS.value_per_point)
    if unit_risk <= 0 or sized.contracts < 1:
        return None
    outcomes = plan["resolved"]
    if sized.single_leg:
        gross = 2.0 if outcomes[2] == "tp" else -1.0
        gross_dollars = gross * sized.contracts * unit_risk
    else:
        tp1_hit = outcomes[0] == "tp"
        tp2_hit = outcomes[1] == "tp"
        gross_dollars = 0.0
        for leg, outcome, rr in zip(sized.legs, outcomes, quantum.RR_TARGETS):
            if outcome == "tp":
                gross_dollars += leg * unit_risk * rr
            elif outcome == "sl":
                if tp1_hit and rr > quantum.RR_TARGETS[0]:
                    # After TP1 the remaining legs are at cost; after TP2 the
                    # runner's stop is stepped to TP1 (+1R).
                    gross_dollars += (leg * unit_risk * quantum.RR_TARGETS[0]
                                      if tp2_hit and rr == quantum.RR_TARGETS[2]
                                      else 0.0)
                else:
                    gross_dollars -= leg * unit_risk
    # Yahoo has no spread.  Keep the known round-turn commission and expected
    # slippage from the production settings, so a free feed is not treated as
    # a zero-cost execution venue.
    costs = sized.contracts * (
        PRODUCTION_SETTINGS.commission_per_contract
        + PRODUCTION_SETTINGS.slippage_ticks * PRODUCTION_SETTINGS.tick_value
    )
    return float(gross_dollars - costs)


def _mgc_daily_pools(requested_risk: float) -> list[np.ndarray]:
    """Build daily dollar pools from the actual MGC strategy plans."""
    key = round(float(requested_risk), 8)
    if key in _MGC_DOLLAR_POOLS:
        return _MGC_DOLLAR_POOLS[key]
    stream_days: list[dict] = []
    all_dates: list[pd.Timestamp] = []
    for _, timeframe in MGC_STREAMS:
        context = _load_mgc_analysis(timeframe)
        frame, result = context["frame"], context["result"]
        # Keep the full Yahoo calendar even when a small risk tier cannot size
        # any of the strategy's stops; that tier then becomes an honest all-zero
        # pool and the room-aware simulator stands down for it where appropriate.
        all_dates.extend((pd.Timestamp(frame["time"].iloc[0]).normalize(),
                          pd.Timestamp(frame["time"].iloc[-1]).normalize()))
        daily: dict[object, float] = {}
        for plan in result["plans"]:
            fill = plan["entry_fill_index"]
            if fill is None:
                continue
            pnl = _mgc_trade_pnl(plan, key)
            if pnl is None:
                continue
            day = pd.Timestamp(frame["time"].iloc[fill]).date()
            daily[day] = daily.get(day, 0.0) + pnl
        stream_days.append(daily)
        all_dates.extend(pd.Timestamp(day) for day in daily)
    start, end = min(all_dates).normalize(), max(all_dates).normalize()
    span = pd.date_range(start, end, freq="D")
    traded_dates = set(day for daily in stream_days for day in daily)
    dates = [day.date() for day in span if day.dayofweek < 5 or day.date() in traded_dates]
    pools_out = []
    for daily in stream_days:
        values = np.asarray([daily.get(day, 0.0) for day in dates], dtype=np.float32)
        pools_out.append(values)
    _MGC_DOLLAR_POOLS[key] = pools_out
    return pools_out


def _draw_streams(streams: list[np.ndarray], corr: float,
                  rng: np.random.Generator) -> np.ndarray:
    """Sample the two MGC daily pools with the configured correlation."""
    shape = (NSIM, MAXDAYS)
    if corr <= 0:
        return sum(rng.choice(pool, size=shape) for pool in streams)
    quantile = rng.random(shape)
    return sum((1 - corr) * rng.choice(pool, size=shape)
               + corr * np.quantile(pool, quantile) for pool in streams)


def _sampled_daily_by_risk(scenario: str, corr: float,
                           rng: np.random.Generator,
                           risks: tuple[float, ...]) -> dict[float, np.ndarray]:
    """Return sampled MGC dollar matrices for every possible risk tier."""
    if scenario != MGC_SCENARIO:
        raise ValueError(f"Future simulator only supports {MGC_SCENARIO!r}; "
                         "Forex/XAUUSD scenarios are not accepted")
    return {float(risk): _draw_streams(_mgc_daily_pools(risk), corr, rng)
            for risk in risks}


def daily_dollars(scenario: str, risk_dollars: float, corr: float,
                  rng: np.random.Generator) -> np.ndarray:
    """(NSIM, MAXDAYS) of portfolio daily P&L in dollars, before any rule."""
    return _sampled_daily_by_risk(scenario, corr, rng,
                                  (float(risk_dollars),))[float(risk_dollars)]


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
    tiers = tuple(sorted({size for _, size in LADDER} | set(RECOVERY_TIERS),
                         reverse=True))
    sampled = _sampled_daily_by_risk(scenario, corr, rng, tiers)

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
        raw_pnl = np.zeros(NSIM)
        for size in tiers:
            raw_pnl = np.where(risk == size, sampled[size][:, day], raw_pnl)
        pnl = np.maximum(raw_pnl, -min(internal_stop, DAILY_LOSS_LIMIT))
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
    tiers = np.array(sorted({size for _, size in LADDER} | set(RECOVERY_TIERS),
                            reverse=True))
    smallest = float(tiers.min())
    sampled = _sampled_daily_by_risk(scenario, corr, rng,
                                     tuple(float(value) for value in tiers))

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

        raw_pnl = np.zeros(NSIM)
        for size in tiers:
            raw_pnl = np.where(risk == size, sampled[float(size)][:, day], raw_pnl)
        pnl = np.where(traded,
                       np.maximum(raw_pnl, -np.minimum(internal_stop, room)),
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

    The source MGC plans keep per-trade timestamps, so an ordered intraday
    truncation is possible in principle -- but the daily MGC pools have already
    summed the day by the time anything gets here, and rebuilding the trade sequence is a
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
    if tuple(scenarios) != (MGC_SCENARIO,):
        raise ValueError(f"Future simulator only supports {MGC_SCENARIO!r}; "
                         "Forex/XAUUSD scenarios are not accepted")
    # Force a data-load before printing the table, so a missing cache fails with
    # the downloader command rather than silently switching to another venue.
    for _, timeframe in MGC_STREAMS:
        _load_mgc_analysis(timeframe)
    info = _MGC_INFO or {}
    stream_counts = ", ".join(
        f"{item['timeframe']} {item['bars']} bars/{item['filled']} fills"
        for item in info.get("streams", [])
    )
    source_note = (f"MGC Yahoo ({info.get('first', '?')} -> {info.get('last', '?')}) | "
                   f"60d smoke sample; {stream_counts}; "
                   "contract-rounded, cost-adjusted")
    print(f"\nTopStep {ACCOUNT_SIZE:,.0f} Combine | target ${PROFIT_TARGET:,.0f} | "
          f"MLL ${MAX_LOSS_LIMIT:,.0f} trailing end-of-day, freezing at "
          f"${TRAIL_FREEZE_AT:,.0f} | DLL ${DAILY_LOSS_LIMIT:,.0f} | "
          f"internal stop ${internal_stop:,.0f} | consistency "
          f"{CONSISTENCY_SHARE:.0%} | corr={corr}"
          + (f" | {require_winning_days} winning days required" if require_winning_days
             else " | no winning-day requirement"))
    print(f"  source: {source_note}")
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
                risk_label = "prod"
            elif risk < 0:
                m = simulate_ladder(scenario, corr, internal_stop,
                                    require_winning_days)
                risk_label = "ladder"
            else:
                m = simulate(scenario, risk, corr, require_winning_days,
                             internal_stop)
                risk_label = f"{risk:.0f}"
            print(f"{m['scenario']:12s}{risk_label:>7s}{m['pass']:8.1%}{m['fail']:8.1%}"
                  f"{m['unresolved']:7.1%}"
                  f"{f'{m['days_med']}/{m['days_p90']}':>12s}"
                  f"{m['dd_med']:8.0f}{m['dd_p95']:8.0f}{m['worst_day']:+11.0f}"
                  f"{m['best_day_med']:+10.0f}", flush=True)


def main() -> None:
    global NSIM

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--risk", type=float, action="append",
                        help="fixed risk per trade in dollars (repeatable; experiment only)")
    parser.add_argument("--corr", type=float, default=0.3)
    parser.add_argument("--nsim", type=int, default=NSIM)
    parser.add_argument("--internal-stop", type=float, default=INTERNAL_DAILY_STOP)
    parser.add_argument("--ladder", action="store_true",
                        help="size with the drawdown ladder instead of flat risk")
    parser.add_argument("--room-aware", action="store_true",
                        help="ladder plus the remaining-room guard, i.e. what the "
                             "bot actually does now (the default)")
    parser.add_argument("--flat", action="store_true",
                        help="run the fixed-risk reference table ($100/$150/$200/$300)")
    parser.add_argument("--require-winning-days", type=int, default=0,
                        help="sources disagree on whether the Combine still "
                             "needs winning days; 0 = target only")
    parser.add_argument("--scenario", action="append",
                        choices=(MGC_SCENARIO,))
    args = parser.parse_args()
    if args.nsim < 100:
        parser.error("--nsim must be at least 100")
    NSIM = args.nsim
    try:
        risks = select_risks(args.risk, ladder=args.ladder,
                             room_aware=args.room_aware, flat=args.flat)
    except ValueError as exc:
        parser.error(str(exc))
    scenarios = args.scenario or (MGC_SCENARIO,)
    try:
        report(risks, scenarios, args.corr, args.require_winning_days,
               args.internal_stop)
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
