"""Portfolio-level FTMO simulation on top of the technique-lab holdout curves.

The technique lab measures one symbol/timeframe at a time in R units. FTMO is
scored on a single account: daily loss, total loss and profit target all apply to
the combined equity. This script joins the per-stream trade series into portfolio
trading days, applies the FTMO 2-Step rules, and reports pass rates plus the
number of trading days a pass takes.

Three calibrations are reported. Each keeps the holdout day-to-day *shape* and
re-bases the per-trade mean to the expectancy measured on that split, so the
pessimistic case answers "what if the recent regime disappears".

    python tools/ftmo_portfolio_sim.py                       # XAUUSD M15+M30 production book
    python tools/ftmo_portfolio_sim.py --risk 0.40           # fixed-risk experiment
    python tools/ftmo_portfolio_sim.py --production --book "XAU M15 + M30"
    python tools/ftmo_portfolio_sim.py --by-year
    python tools/ftmo_portfolio_sim.py --plot

Every simulation resamples the same holdout trades, so it quantifies path risk
given the edge is real. It cannot validate the edge itself.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import dynamic_risk as forex_dynamic  # noqa: E402
from strategy import backtest_reporting, config  # noqa: E402


def _load_bot_settings_module():
    """Import bot/settings.py without putting bot/forex on sys.path.

    Prepending that directory would shadow any other top-level `settings`
    module for everything imported afterwards, which is not a trade worth
    making to read one file.
    """
    import importlib.util

    path = ROOT / "bot" / "settings.py"
    spec = importlib.util.spec_from_file_location("bot_settings", path)
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves annotations through sys.modules, so register first.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bot_settings = _load_bot_settings_module()
PRODUCTION_SETTINGS = bot_settings.load()

NSIM, MAXDAYS = 20_000, 400
TECHNIQUE_OVERRIDE: str | None = None
# "production" resolves the exit from the bot's own settings; "selected" falls
# back to the validation pick in each report.
TECHNIQUE_SOURCE: str = "production"
DAILY_LIMIT = PRODUCTION_SETTINGS.daily_loss_percent
MAX_LIMIT = PRODUCTION_SETTINGS.max_loss_percent
STEP_TARGETS = (10.0, 5.0)
INTERNAL_DAILY_STOP = PRODUCTION_SETTINGS.internal_daily_stop_percent
MIN_TRADING_DAYS = PRODUCTION_SETTINGS.min_trading_days

# stream -> (symbol, timeframe, extra cost R/trade on top of the lab's own model).
#
# The extra used to carry 0.02-0.08R of commission and slippage, because the lab
# charged spread alone. `technique_lab._cost_r` now models all three from
# `config.COSTS`, so anything here would be charged twice: it is 0.0 and stays 0.0
# unless there is a cost the model genuinely does not cover.
#
# The technique is not pinned and not taken from the report's own validation
# pick either: both were wrong in the same way. It is resolved from the bot's
# `exit_mode` against anchored capital, so the book simulated here is the book
# the live bot would trade. See `production_technique`.
STREAMS: dict[str, tuple[str, str, float]] = {
    "XAU M30": ("XAUUSD", "M30", 0.0),
    "XAU M15": ("XAUUSD", "M15", 0.0),
    "XAU H1": ("XAUUSD", "H1", 0.0),
    "EUR M30": ("EURUSD", "M30", 0.0),
    "BTC M5": ("BTCUSD", "M5", 0.0),
}

BOOKS: dict[str, list[str]] = {
    "XAU M30 only": ["XAU M30"],
    "XAU M15 only": ["XAU M15"],
    "XAU M30 + H1": ["XAU M30", "XAU H1"],
    "XAU M15 + M30": ["XAU M15", "XAU M30"],
    "XAU M15+M30 + EUR M30": ["XAU M15", "XAU M30", "EUR M30"],
    "XAU M15+M30+H1 + EUR": ["XAU M15", "XAU M30", "XAU H1", "EUR M30"],
    "+ BTC M5": ["XAU M15", "XAU M30", "XAU H1", "EUR M30", "BTC M5"],
}
# The live Forex bot is the XAUUSD M15+M30 book.  Other books remain useful
# research comparisons, but must be requested explicitly so an unqualified
# production simulation can never quietly mix another symbol into the result.
DEFAULT_PRODUCTION_BOOK = "XAU M15 + M30"

SCENARIOS = (("holdout", "HOLDOUT mean (current regime)"),
             ("validation", "VALIDATION mean (base case)"),
             ("train", "TRAIN mean (older / flat regime)"))

# ---------------------------------------------------------------------------
# Chart pack
# ---------------------------------------------------------------------------
# Kept out of FTMO_CHART_DIR because plot_ftmo_charts.py rmtree's that root.
PLOT_DIR = config.OUTPUT_DIR / "charts" / "ftmo_plan"
BG, PANEL, GRID = "#0b0f14", "#111821", "#2a3441"
TEXT, MUTED = "#e5edf5", "#9eabb8"
# Categorical slots and the diverging pair, validated for the dark surface with
# the dataviz palette checker (adjacent CVD dE 8.4, normal-vision dE 19.3).
BLUE, ORANGE, AQUA, YELLOW, MAGENTA = "#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"
POS, NEG = AQUA, "#e66767"
SCENARIO_COLOR = {"holdout": AQUA, "validation": BLUE, "train": ORANGE}
SCENARIO_SHORT = {"holdout": "Current regime", "validation": "Base", "train": "Older / flat regime"}
PLOT_BOOKS = ["XAU M30 only", "XAU M15 only", "XAU M15 + M30",
              "XAU M15+M30 + EUR M30", "+ BTC M5"]
# Charts follow the same production path as the command-line report by default.
# An explicit ``--risk`` turns them into a fixed-risk reference chart.
PLOT_RISK: float | None = None


def plot_risk_label() -> str:
    return ("production dynamic ladder" if PLOT_RISK is None
            else f"fixed {PLOT_RISK:.2f}%/trade")


# The lab name for each exit the bot can resolve to. `be_33_33_34` is the same
# three-leg BE + TP2-to-TP1 step the lab measures as be_after_tp1_33_33_34.
BOT_EXIT_TO_TECHNIQUE = {
    "fixed_tp3": "fixed_tp3",
    "be_33_33_34": "be_after_tp1_33_33_34",
}


def production_technique() -> str:
    """The exit the live bot would actually run on this account.

    The simulation used to call `select_technique`, which is the validation pick
    per report -- `fixed_tp3` on M15. The bot never asks that question: it
    resolves `exit_mode` against anchored capital, so a $50K account runs the
    three-leg BE exit on every timeframe. Simulating the report pick priced a
    book nobody trades.
    """
    settings = PRODUCTION_SETTINGS
    capital = settings.initial_balance or 0.0
    mode = settings.resolved_exit_mode(capital)
    if mode == "auto":                      # sizing decides per trade; the lab cannot
        mode = "be_33_33_34"                # model that, so price the richer exit
    try:
        return BOT_EXIT_TO_TECHNIQUE[mode]
    except KeyError:
        raise SystemExit(f"no technique-lab equivalent for exit_mode={mode!r}") from None


def resolve_technique(report: dict) -> str:
    if TECHNIQUE_OVERRIDE:
        return TECHNIQUE_OVERRIDE
    if TECHNIQUE_SOURCE == "production":
        return production_technique()
    return backtest_reporting.select_technique(report)


def load_stream(key: str, scenario: str) -> tuple[np.ndarray, np.ndarray, float, float]:
    """Daily R totals and an explicit traded-day mask over the holdout span."""
    symbol, timeframe, cost = STREAMS[key]
    report = backtest_reporting.load_report(
        backtest_reporting.report_path(symbol, timeframe))
    technique = resolve_technique(report)
    curve = report["holdout_all_curves_r"]
    equity = np.asarray(curve["equity"][technique], dtype=float)
    trade_r = np.diff(np.concatenate([[0.0], equity]))
    metrics = report["techniques"][technique]
    if scenario == "decay_zero":
        trade_r = trade_r - trade_r.mean()
    elif scenario != "holdout":
        trade_r = trade_r - trade_r.mean() + metrics[scenario]["expectancy_r"]
    trade_r = trade_r - cost

    stamps = pd.to_datetime(curve["time"])
    series = pd.Series(trade_r, index=stamps)
    by_day = series.groupby(series.index.date).sum()
    span = pd.date_range(stamps.min().normalize(), stamps.max().normalize(), freq="D")
    traded = set(by_day.index)
    # Metals and FX are 24/5, but the feed does open on Sunday evening broker time.
    span = pd.DatetimeIndex([d for d in span if d.dayofweek < 5 or d.date() in traded])
    days = pd.Series(0.0, index=span.date)
    days.loc[list(by_day.index)] = by_day.values
    traded_mask = pd.Series(False, index=span.date)
    traded_mask.loc[list(by_day.index)] = True
    expectancy = (0.0 if scenario == "decay_zero"
                  else metrics[scenario]["expectancy_r"] - cost)
    return (days.to_numpy(), traded_mask.to_numpy(), len(series) / len(days),
            expectancy)


def _sample_pool(pool, shape: tuple[int, int], rng,
                 shared_quantile: np.ndarray | None = None,
                 correlation: float = 0.0):
    """Sample values and their source-day mask without inferring trades from P&L."""
    values, traded, _weight, _expectancy = pool
    indices = rng.integers(0, len(values), size=shape)
    independent = values[indices]
    independent_traded = traded[indices]
    if shared_quantile is None:
        return independent, independent_traded
    order = np.argsort(values)
    shared_indices = order[np.minimum(
        (shared_quantile * len(order)).astype(int), len(order) - 1)]
    correlated = values[shared_indices]
    correlated_traded = traded[shared_indices]
    return ((1.0 - correlation) * independent + correlation * correlated,
            independent_traded | correlated_traded)


def _sampled_pools(pools, corr: float, rng):
    """Sample each stream once so fixed and production risk use identical paths."""
    shape = (NSIM, MAXDAYS)
    if corr <= 0:
        return [_sample_pool(pool, shape, rng) for pool in pools]
    quantile = rng.random(shape)
    return [_sample_pool(pool, shape, rng, quantile, corr) for pool in pools]


def portfolio_days(pools, risk_pct: float, corr: float, rng,
                   return_traded: bool = False):
    """(NSIM, MAXDAYS) returns, optionally with explicit traded-day mask."""
    samples = _sampled_pools(pools, corr, rng)
    total = sum(values for values, _traded in samples)
    traded = np.logical_or.reduce([mask for _values, mask in samples])
    result = total * risk_pct
    return (result, traded) if return_traded else result


def _production_desired_risk(equity: np.ndarray, high_water: np.ndarray,
                             settings=PRODUCTION_SETTINGS) -> np.ndarray:
    """Vector form of `engine.dynamic_risk.decide` for a 100% equity base."""
    drawdown = np.maximum(0.0, high_water - equity)
    if not settings.dynamic_risk_enabled:
        return np.full(equity.shape, settings.risk_percent, dtype=float)
    return np.select(
        [drawdown < settings.dynamic_risk_dd1_percent,
         drawdown < settings.dynamic_risk_dd2_percent,
         drawdown < settings.dynamic_risk_dd3_percent],
        [settings.dynamic_risk_max_percent,
         settings.dynamic_risk_tier2_percent,
         settings.dynamic_risk_tier3_percent],
        default=settings.risk_percent,
    ).astype(float)


def production_portfolio_days(pools, corr: float, rng,
                              settings=PRODUCTION_SETTINGS):
    """Daily P&L using the live bot's dynamic tiers and exposure cap.

    The source data is still daily, so this cannot reproduce intraday order
    sequencing. It does, however, use the same settings and tier-selection
    rules as production instead of silently pricing every day at 0.40%.
    """
    samples = _sampled_pools(pools, corr, rng)
    streams = [values for values, _traded in samples]
    masks = [traded for _values, traded in samples]
    paths = streams[0].shape[0]
    balance = np.full(paths, 100.0)
    high_water = balance.copy()
    result = np.zeros((paths, MAXDAYS))
    traded_result = np.zeros((paths, MAXDAYS), dtype=bool)
    tiers = np.asarray(forex_dynamic.fitting_tiers(
        settings, settings.dynamic_risk_max_percent), dtype=float)

    for day in range(MAXDAYS):
        active = np.sum([mask[:, day] for mask in masks], axis=0)
        desired = _production_desired_risk(balance, high_water, settings)
        # Mirror the live projected guards: the aggregate setup risk must fit
        # the open-risk cap and the remaining static max-loss room. The daily
        # internal room is the tighter configured stop at the start of each
        # synthetic day; intraday sequencing remains an explicit approximation.
        account_room = np.maximum(0.0, balance + MAX_LIMIT)
        total_room = np.minimum(settings.max_open_risk_percent, account_room)
        total_room = np.minimum(total_room, settings.internal_daily_stop_percent)
        ceiling = np.divide(total_room,
                            np.maximum(active, 1), dtype=float)
        chosen = np.zeros(paths, dtype=float)
        if settings.dynamic_risk_enabled and settings.dynamic_risk_fit_remaining:
            for tier in tiers:
                fits = ((chosen == 0.0) & (active > 0)
                        & (tier <= desired + 1e-12)
                        & (tier <= ceiling + 1e-12))
                chosen = np.where(fits, tier, chosen)
        else:
            chosen = np.where((active > 0) & (desired <= ceiling + 1e-12),
                              desired, 0.0)
        daily = sum(values[:, day] * chosen for values in streams)
        daily = np.maximum(daily, -min(settings.internal_daily_stop_percent,
                                       settings.daily_loss_percent))
        result[:, day] = daily
        traded_result[:, day] = (active > 0) & (chosen > 0)
        balance += daily
        high_water = np.maximum(high_water, balance)
    return result, traded_result


def path_drawdown(days_pct: np.ndarray) -> np.ndarray:
    """Deepest peak-to-trough equity drop of each path, in percent.

    Closed-day equity only, like every other number here: it excludes floating
    drawdown inside a day, and it does not model the dynamic-risk ladder cutting
    size as the drop deepens.
    """
    cum = np.cumsum(days_pct, axis=1)
    start = np.zeros((len(cum), 1))
    peak = np.maximum.accumulate(np.concatenate([start, cum], axis=1), axis=1)[:, 1:]
    return (peak - cum).max(1)


INTERNAL_DAILY_STOP = 1.50      # percent; the live bot stands down here
MIN_TRADING_DAYS = 4            # FTMO 2-Step, applied to EACH phase


def apply_internal_stop(days_pct: np.ndarray,
                        internal_stop: float = INTERNAL_DAILY_STOP) -> np.ndarray:
    """Clip each day's NET loss at the internal daily stop the live bot enforces.

    The simulation used to model only FTMO's own 5% daily breach, so it kept
    trading tails the live bot would never see: a -3% day is not a thing this
    system can produce, because it stands down at -1.50%.

    APPROXIMATION, in the same way the futures simulator's is: the stop is a
    rule about the ORDER of trades inside a day, and this only sees the day's
    net. A day that goes -1.8% and recovers to -0.5% is left alone here, while
    the live bot would have stopped at -1.50% and never taken the recovery. The
    three-consecutive-loss stand-down is likewise not modelled -- it needs the
    trade sequence, which `load_stream` has already summed away.

    Both omissions point the same way: results are OPTIMISTIC about recoveries.
    """
    return np.maximum(days_pct, -abs(internal_stop))


@dataclass(frozen=True)
class PhaseOutcome:
    """Vectorised verdict plus the final day that existed for each path."""

    passed: np.ndarray
    day: np.ndarray
    breached: np.ndarray
    resolution_index: np.ndarray


def phase_outcome(days_pct: np.ndarray, target: float,
                  min_days: int = MIN_TRADING_DAYS,
                  traded_mask: np.ndarray | None = None) -> PhaseOutcome:
    """Resolve a phase and retain its inclusive zero-based stopping index.

    `min_days` is FTMO's minimum trading day count and applies to EACH phase
    independently. Without it a path that reached +10% on day one passed on day
    one, which is not an outcome FTMO offers: the objective is the target AND
    four trading days, so the earliest possible pass is day four.
    """
    cum = np.cumsum(days_pct, axis=1)
    never = days_pct.shape[1] + 10
    first = lambda mask: np.where(mask.any(1), mask.argmax(1), never)
    if traded_mask is None:
        # Backwards-compatible direct-call behavior. Production simulation
        # passes the explicit source-day mask from `portfolio_days` below.
        traded_mask = days_pct != 0.0
    traded_mask = np.asarray(traded_mask, dtype=bool)
    if traded_mask.shape != days_pct.shape:
        raise ValueError("traded_mask must have the same shape as days_pct")
    traded = np.cumsum(traded_mask, axis=1)
    eligible = cum >= target
    if min_days:
        eligible &= traded >= min_days
    hit = first(eligible)
    blown = first(cum <= -MAX_LIMIT)
    daily = first(days_pct <= -DAILY_LIMIT)
    breach = np.minimum(blown, daily)
    passed = hit < breach
    breached = breach < np.minimum(hit, never)
    # Unresolved paths exist through the full generated horizon. Resolved paths
    # stop on the first pass/breach day; generated tail rows never happened.
    resolution = np.minimum(np.minimum(hit, breach), days_pct.shape[1] - 1)
    return PhaseOutcome(passed, hit + 1, breached, resolution)


def resolve_phase(days_pct: np.ndarray, target: float,
                  min_days: int = MIN_TRADING_DAYS,
                  traded_mask: np.ndarray | None = None):
    """Compatibility tuple: (passed, pass day, breached)."""
    outcome = phase_outcome(days_pct, target, min_days, traded_mask)
    return outcome.passed, outcome.day, outcome.breached


def freeze_after_resolution(days_pct: np.ndarray,
                            resolution_index: np.ndarray) -> np.ndarray:
    """Replace generated post-resolution tail days with zero P&L."""
    resolution = np.asarray(resolution_index, dtype=int)
    if resolution.shape != (days_pct.shape[0],):
        raise ValueError("resolution_index must contain one index per path")
    mask = np.arange(days_pct.shape[1])[None, :] <= resolution[:, None]
    return np.where(mask, days_pct, 0.0)


def worst_day_before_resolution(days_pct: np.ndarray,
                                resolution_index: np.ndarray) -> np.ndarray:
    """Worst day that actually occurred, excluding generated tail rows."""
    resolution = np.asarray(resolution_index, dtype=int)
    if resolution.shape != (days_pct.shape[0],):
        raise ValueError("resolution_index must contain one index per path")
    mask = np.arange(days_pct.shape[1])[None, :] <= resolution[:, None]
    return np.where(mask, days_pct, np.inf).min(axis=1)


def two_step_breach_mask(step1: PhaseOutcome,
                         step2: PhaseOutcome) -> np.ndarray:
    """Breach either Step 1 or Step 2, counting Step 2 only if it was reached."""
    return step1.breached | (step1.passed & step2.breached)


def simulate(keys: list[str], scenario: str, risk_pct: float | None,
             corr: float, seed: int = 41) -> dict:
    rng = np.random.default_rng(seed)
    pools = [load_stream(key, scenario) for key in keys]
    if risk_pct is None:
        step1_raw, traded1 = production_portfolio_days(pools, corr, rng)
    else:
        step1_raw, traded1 = portfolio_days(pools, risk_pct, corr, rng,
                                            return_traded=True)
    step1 = apply_internal_stop(step1_raw)
    outcome1 = phase_outcome(step1, STEP_TARGETS[0], traded_mask=traded1)
    resolved_step1 = freeze_after_resolution(step1, outcome1.resolution_index)
    dd = path_drawdown(resolved_step1)
    # Verification is a fresh account, so draw an independent set of paths.
    if risk_pct is None:
        step2_raw, traded2 = production_portfolio_days(pools, corr, rng)
    else:
        step2_raw, traded2 = portfolio_days(pools, risk_pct, corr, rng,
                                            return_traded=True)
    outcome2 = phase_outcome(apply_internal_stop(step2_raw), STEP_TARGETS[1],
                             traded_mask=traded2)
    both = outcome1.passed & outcome2.passed
    breached = two_step_breach_mask(outcome1, outcome2)
    total = np.where(both, outcome1.day + outcome2.day, 0)
    weights = [pool[2] for pool in pools]
    pick = lambda values, q: int(np.quantile(values, q)) if len(values) else -1
    return {
        "trades_per_day": sum(weights),
        "risk_mode": "production" if risk_pct is None else f"fixed {risk_pct:.2f}%",
        "expectancy_r": float(np.average([pool[3] for pool in pools], weights=weights)),
        "r_per_day": sum(pool[0].mean() for pool in pools),
        "step1": outcome1.passed.mean(), "breach": breached.mean(),
        "step1_breach": outcome1.breached.mean(),
        "step2_breach": (outcome1.passed & outcome2.breached).mean(),
        "two_step": both.mean(),
        "d1_med": pick(outcome1.day[outcome1.passed], .5),
        "d1_p90": pick(outcome1.day[outcome1.passed], .9),
        "total_med": pick(total[both], .5), "total_p90": pick(total[both], .9),
        "worst_day": float(np.quantile(
            worst_day_before_resolution(step1, outcome1.resolution_index), .05)),
        "dd_med": float(np.median(dd)), "dd_p99": float(np.quantile(dd, .99)),
        "dd_over_limit": float((dd > MAX_LIMIT).mean()),
    }


def report_books(risks=(0.25, 0.40, 0.50), corr: float = 0.3,
                 book_names: list[str] | None = None,
                 production: bool = False) -> None:
    selected_books = book_names or list(BOOKS)
    for scenario, label in SCENARIOS:
        print(f"\n{'=' * 118}\n{label}   [FTMO 2-Step: {STEP_TARGETS[0]:.0f}% then "
              f"{STEP_TARGETS[1]:.0f}%, daily {DAILY_LIMIT:.0f}%, max {MAX_LIMIT:.0f}%, "
              f"corr={corr}, internal stop {INTERNAL_DAILY_STOP:.2f}%, "
              f"{MIN_TRADING_DAYS} min trading days]\n{'=' * 118}")
        print("  daily-net approximation: the internal stop clips a day\'s NET "
              "result, not its trade sequence, and the 3-loss stand-down is not "
              "modelled, so results are optimistic.")
        print("  breach = account failure in either Step 1 or Step 2; drawdown and "
              "worst-day statistics stop when Step 1 resolves.")
        print(f"{'book':24s} {'risk':>5s} {'trd/d':>6s} {'expR':>7s} {'R/day':>7s} "
              f"{'step1':>7s} {'breach':>7s} {'2-step':>7s} {'days step1':>12s} "
              f"{'days total':>12s} {'worst day':>10s} {'dd med':>7s} {'dd p99':>7s} "
              f"{f'dd>{MAX_LIMIT:.0f}%':>7s}")
        for name in selected_books:
            keys = BOOKS[name]
            for risk in (None,) if production else risks:
                m = simulate(keys, scenario, risk, corr)
                risk_label = "prod" if risk is None else f"{risk:.2f}"
                print(f"{name:24s} {risk_label:>5s} {m['trades_per_day']:6.1f} "
                      f"{m['expectancy_r']:+7.3f} {m['r_per_day']:+7.3f} {m['step1']:7.1%} "
                      f"{m['breach']:7.1%} {m['two_step']:7.1%} "
                      f"{f'{m['d1_med']}/{m['d1_p90']}':>12s} "
                      f"{f'{m['total_med']}/{m['total_p90']}':>12s} {m['worst_day']:+10.2f}%"
                      f"{m['dd_med']:7.2f} {m['dd_p99']:7.2f} {m['dd_over_limit']:7.1%}")


def report_by_year(targets=(("XAUUSD", "H1"), ("XAUUSD", "M30"),
                            ("EURUSD", "M30"))) -> None:
    """Re-run the engine and split expectancy by calendar year to expose regime risk.

    Uses the same exit the report selected on validation, and the same cost model,
    so a year here is comparable with a split there. It used to assume
    BE + 33/33/34 and add its own commission estimate on top of the lab's.
    """
    from strategy import quantum, technique_lab as lab

    for symbol, timeframe in targets:
        report = backtest_reporting.load_report(
            backtest_reporting.report_path(symbol, timeframe))
        technique = resolve_technique(report)
        payoff_of = lab.payoff_for(technique)
        frame = pd.read_csv(config.MARKET_DATA_DIR / symbol / f"{timeframe}.csv",
                            parse_dates=["time"])
        result = quantum.analyse(frame, timeframe)
        data = result["data"]
        rows = []
        for plan in result["plans"]:
            if plan["entry_fill_index"] is None:
                continue
            payoff = payoff_of(plan) - lab._cost_r(data, plan, symbol)
            rows.append({"time": pd.Timestamp(data["time"].iloc[plan["signal_index"]]),
                         "r": payoff})
        trades = pd.DataFrame(rows)
        trades["year"] = trades["time"].dt.year
        grouped = trades.groupby("year")["r"].agg(["count", "mean", "sum"])
        grouped["win_rate"] = (trades.assign(w=trades.r > 0)
                               .groupby("year")["w"].mean() * 100)
        grouped.columns = ["trades", "expectancy_r", "net_r", "win_rate"]
        print(f"\n=== {symbol} {timeframe} {technique} — {len(trades)} trades ===")
        print(grouped.round(3).to_string())


def style_axis(axis, xgrid: bool = False, ygrid: bool = True) -> None:
    axis.set_facecolor(PANEL)
    for enabled, which in ((xgrid, "x"), (ygrid, "y")):
        if enabled:
            axis.grid(True, axis=which, color=GRID, linewidth=.7, alpha=.7)
    axis.set_axisbelow(True)
    axis.spines[["top", "right"]].set_visible(False)
    axis.spines[["left", "bottom"]].set_color("#586574")
    axis.tick_params(colors=MUTED)
    axis.xaxis.label.set_color(MUTED)
    axis.yaxis.label.set_color(MUTED)
    axis.title.set_color(TEXT)


def save_figure(fig, name: str) -> Path:
    path = PLOT_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.patch.set_facecolor(BG)
    fig.savefig(path, dpi=160, facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    print(f"[PLOTTED] {path}")
    return path


def collect(corr: float) -> dict[tuple[str, str], dict]:
    return {(book, scenario): simulate(BOOKS[book], scenario, PLOT_RISK, corr)
            for book in PLOT_BOOKS for scenario, _ in SCENARIOS}


def plot_days_to_pass(results: dict, corr: float) -> None:
    """Median-to-p90 range of trading days needed for both steps, per book."""
    fig, axes = plt.subplots(1, 3, figsize=(16, 6), sharey=True, constrained_layout=True)
    y = np.arange(len(PLOT_BOOKS))
    limit = max(results[key]["total_p90"] for key in results) * 1.16
    for axis, (scenario, _) in zip(axes, SCENARIOS):
        style_axis(axis, xgrid=True, ygrid=False)
        color = SCENARIO_COLOR[scenario]
        for row, book in enumerate(PLOT_BOOKS):
            m = results[(book, scenario)]
            median, p90 = m["total_med"], m["total_p90"]
            if median < 0:
                axis.text(8, row, "target not reached", color=MUTED, va="center", fontsize=10)
                continue
            # 2px surface gap between the median bar and its p90 extension.
            axis.barh(row, median, height=.52, color=color)
            axis.barh(row, p90 - median, height=.52, left=median, color=color, alpha=.3,
                      linewidth=2, edgecolor=PANEL)
            text = f"{median} → {p90}"
            if p90 + .17 * limit < limit:
                axis.text(p90 + .015 * limit, row, text, color=TEXT, va="center", fontsize=10)
            else:  # keep the label inside rather than overflowing the panel
                axis.text(median - .015 * limit, row, text, color=BG, va="center",
                          ha="right", fontsize=10)
        axis.set_yticks(y, PLOT_BOOKS)
        axis.invert_yaxis()
        axis.set_xlim(0, limit)
        axis.set_xlabel("Trading days")
        axis.set_title(SCENARIO_SHORT[scenario], loc="left", fontsize=13, color=color)
    axes[0].set_ylabel("")
    fig.suptitle("Trading days to clear both FTMO steps  ·  bar = median, faded = 90th percentile",
                 color=TEXT, fontsize=15, x=.007, ha="left")
    fig.text(.5, -.03, f"{plot_risk_label()}  ·  corr {corr}  ·  "
                       f"21 trading days ≈ 1 month  ·  commission and slippage deducted",
             color=MUTED, fontsize=11, ha="center")
    save_figure(fig, "days_to_pass.png")


def plot_pass_rate(results: dict, corr: float) -> None:
    """Probability of clearing Step 1, by book and calibration."""
    fig, axis = plt.subplots(figsize=(14, 6.4), constrained_layout=True)
    style_axis(axis)
    x = np.arange(len(PLOT_BOOKS))
    width = .26
    for offset, (scenario, _) in enumerate(SCENARIOS):
        values = [results[(book, scenario)]["step1"] * 100 for book in PLOT_BOOKS]
        bars = axis.bar(x + (offset - 1) * width, values, width,
                        color=SCENARIO_COLOR[scenario], label=SCENARIO_SHORT[scenario],
                        linewidth=2, edgecolor=PANEL)
        axis.bar_label(bars, labels=[f"{v:.0f}%" for v in values], padding=3,
                       color=TEXT, fontsize=9)
    axis.set_xticks(x, PLOT_BOOKS)
    axis.set_ylim(0, 112)
    axis.set_ylabel("Probability of clearing Step 1 (%)")
    axis.set_title("Step 1 (+10%) pass rate — the older-regime bars are the honest downside",
                   loc="left", fontsize=15)
    axis.legend(frameon=False, ncol=3, labelcolor=MUTED)
    axis.text(0, -14, f"{plot_risk_label()} · corr {corr} · commission and slippage "
                      f"deducted · 20,000 simulated paths per bar",
              color=MUTED, fontsize=10, transform=axis.get_yaxis_transform())
    save_figure(fig, "pass_rate.png")


def plot_expectancy_by_year(streams=(("XAU H1", "XAUUSD H1"), ("XAU M30", "XAUUSD M30"),
                                     ("EUR M30", "EURUSD M30"))) -> None:
    """Per-year expectancy — the regime-dependence evidence."""
    from strategy import quantum, technique_lab as lab

    fig, axes = plt.subplots(1, 3, figsize=(16, 5.4), constrained_layout=True)
    for axis, (key, title) in zip(axes, streams):
        symbol, timeframe, cost = STREAMS[key]
        report = backtest_reporting.load_report(
            backtest_reporting.report_path(symbol, timeframe))
        technique = resolve_technique(report)
        payoff_of = lab.payoff_for(technique)
        frame = pd.read_csv(config.MARKET_DATA_DIR / symbol / f"{timeframe}.csv",
                            parse_dates=["time"])
        result = quantum.analyse(frame, timeframe)
        data = result["data"]
        rows = [{"year": pd.Timestamp(data["time"].iloc[p["signal_index"]]).year,
                 "r": payoff_of(p) - lab._cost_r(data, p, symbol) - cost}
                for p in result["plans"] if p["entry_fill_index"] is not None]
        yearly = pd.DataFrame(rows).groupby("year")["r"].agg(["mean", "count"])

        style_axis(axis)
        colors = [POS if value >= 0 else NEG for value in yearly["mean"]]
        bars = axis.bar(yearly.index.astype(str), yearly["mean"], color=colors,
                        linewidth=2, edgecolor=PANEL)
        # Sign is carried by direction and by the value label, not colour alone.
        axis.bar_label(bars, labels=[f"{v:+.3f}" for v in yearly["mean"]], padding=3,
                       color=TEXT, fontsize=9)
        axis.axhline(0, color=MUTED, linewidth=1)
        axis.set_ylim(min(yearly["mean"].min() * 1.5, -0.09), yearly["mean"].max() * 1.35)
        axis.set_ylabel("Expectancy (R per trade)")
        axis.set_title(f"{title}  ·  {int(yearly['count'].sum())} trades", loc="left", fontsize=13)
    fig.suptitle("Expectancy by calendar year — negative years are why the edge is regime-dependent",
                 color=TEXT, fontsize=15, x=.007, ha="left")
    save_figure(fig, "expectancy_by_year.png")


def plot_equity_fan(book: str = "XAU M15 + M30", corr: float = 0.3, horizon: int = 140) -> None:
    """Percentile fan of simulated account equity against the FTMO limits."""
    fig, axes = plt.subplots(1, 3, figsize=(16, 5.6), sharey=True, constrained_layout=True)
    rng = np.random.default_rng(97)
    for axis, (scenario, _) in zip(axes, SCENARIOS):
        pools = [load_stream(key, scenario) for key in BOOKS[book]]
        if PLOT_RISK is None:
            daily, _traded = production_portfolio_days(pools, corr, rng)
        else:
            daily = portfolio_days(pools, PLOT_RISK, corr, rng)
        paths = np.cumsum(daily[:, :horizon], axis=1)
        days = np.arange(1, horizon + 1)
        color = SCENARIO_COLOR[scenario]
        style_axis(axis)
        for lo, hi, alpha in ((5, 95, .14), (25, 75, .26)):
            axis.fill_between(days, np.percentile(paths, lo, axis=0),
                              np.percentile(paths, hi, axis=0), color=color, alpha=alpha,
                              linewidth=0)
        median = np.percentile(paths, 50, axis=0)
        axis.plot(days, median, color=color, linewidth=2)
        axis.axhline(0, color=MUTED, linewidth=.8)
        axis.axhline(10, color=TEXT, linewidth=1.2, linestyle="--", alpha=.65)
        axis.axhline(-10, color=NEG, linewidth=1.2, linestyle="--", alpha=.8)
        crossing = np.argmax(median >= 10)
        if median[crossing] >= 10:
            axis.plot(crossing + 1, 10, "o", color=color, markersize=9,
                      markeredgecolor=PANEL, markeredgewidth=2)
            axis.annotate(f"median hits +10% on day {crossing + 1}", xy=(crossing + 1, 10),
                          xytext=(10, -30), textcoords="offset points", color=TEXT, fontsize=10)
        axis.set_xlabel("Trading day")
        axis.set_title(SCENARIO_SHORT[scenario], loc="left", fontsize=13)
    axes[0].set_ylabel("Account return (%)")
    axes[0].text(2, 10.6, "FTMO Step 1 target +10%", color=TEXT, fontsize=10)
    axes[0].text(2, -9.4, "FTMO max loss −10%", color=NEG, fontsize=10)
    fig.suptitle(f"Simulated equity paths · {book} · {plot_risk_label()}  "
                 f"(median line, 25–75 and 5–95 percentile bands, no early stop)",
                 color=TEXT, fontsize=14, x=.007, ha="left")
    save_figure(fig, "equity_fan.png")


def plot_cost_sensitivity(keys=("XAU M30", "XAU M15", "XAU H1", "EUR M30", "BTC M5")) -> None:
    """How much commission + slippage each stream can absorb before its edge dies."""
    colors = (BLUE, ORANGE, AQUA, YELLOW, MAGENTA)
    costs = np.linspace(0, 0.18, 60)
    fig, axis = plt.subplots(figsize=(13, 6.8), constrained_layout=True)
    style_axis(axis)
    ends = []
    for key, color in zip(keys, colors):
        symbol, timeframe, own_cost = STREAMS[key]
        report = backtest_reporting.load_report(
            backtest_reporting.report_path(symbol, timeframe))
        technique = resolve_technique(report)
        base = report["techniques"][technique]["validation"]["expectancy_r"]
        axis.plot(costs, base - costs, color=color, linewidth=2)
        axis.plot(own_cost, base - own_cost, "o", color=color, markersize=9,
                  markeredgecolor=PANEL, markeredgewidth=2)
        ends.append([base - costs[-1], key, color])
    # Push end labels apart so near-identical lines stay readable.
    ends.sort()
    gap = .022
    for index in range(1, len(ends)):
        ends[index][0] = max(ends[index][0], ends[index - 1][0] + gap)
    for y_position, key, color in ends:
        axis.text(costs[-1] + .004, y_position, key, color=color, va="center", fontsize=10)
    axis.axhline(0, color=NEG, linewidth=1.2, linestyle="--", alpha=.8)
    axis.set_xlim(0, .215)
    axis.set_xlabel("Extra cost per trade beyond backtest spread (R)")
    axis.set_ylabel("Net expectancy (R per trade)")
    axis.set_title("Cost tolerance per stream — dot marks the cost this study assumed",
                   loc="left", fontsize=15)
    axis.text(.001, -.052, "Below the dashed line the stream loses money. Validation-split "
                           "expectancy is the starting point.", color=MUTED, fontsize=10)
    save_figure(fig, "cost_sensitivity.png")


def plot_all(corr: float) -> None:
    plt.style.use("dark_background")
    results = collect(corr)
    plot_days_to_pass(results, corr)
    plot_pass_rate(results, corr)
    plot_equity_fan(corr=corr)
    plot_cost_sensitivity()
    plot_expectancy_by_year()
    print(f"plot_set_complete dir={PLOT_DIR} risk={plot_risk_label()}")


def main() -> None:
    global NSIM, TECHNIQUE_OVERRIDE, TECHNIQUE_SOURCE, PLOT_RISK

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--by-year", action="store_true",
                        help="print per-year expectancy instead of the book simulation")
    parser.add_argument("--plot", action="store_true",
                        help=f"render the chart pack into {PLOT_DIR.name}/")
    parser.add_argument("--corr", type=float, default=0.3,
                        help="cross-stream correlation for the portfolio draw")
    parser.add_argument("--book", choices=tuple(BOOKS),
                        help="simulate one book instead of every comparison")
    parser.add_argument("--risk", type=float,
                        help="fixed-risk experiment; default is the production dynamic ladder")
    parser.add_argument("--nsim", type=int, default=NSIM,
                        help=f"Monte Carlo paths (default {NSIM:,})")
    parser.add_argument("--production", action="store_true",
                        help="use the live bot's dynamic risk tiers and exposure cap")
    parser.add_argument(
        "--technique",
        choices=("production", "selected", "fixed_tp3", "be_after_tp1_33_33_34"),
        default="production",
        help="exit policy; default resolves it from the bot's own settings",
    )
    args = parser.parse_args()
    if args.nsim < 100:
        parser.error("--nsim must be at least 100")
    if args.risk is not None and args.risk <= 0:
        parser.error("--risk must be positive")
    if args.production and args.risk is not None:
        parser.error("--production and --risk are mutually exclusive")
    # The command-line report is a production forecast by default. Passing
    # `--risk` explicitly remains the fixed-risk experiment mode.
    production_mode = args.production or args.risk is None
    NSIM = args.nsim
    PLOT_RISK = args.risk
    TECHNIQUE_SOURCE = args.technique
    TECHNIQUE_OVERRIDE = (None if args.technique in ("production", "selected")
                          else args.technique)
    if TECHNIQUE_SOURCE == "production":
        print(f"exit resolved from bot settings: {production_technique()}")
    if args.plot:
        plot_all(args.corr)
    elif args.by_year:
        report_by_year()
    else:
        report_books(
            risks=(args.risk,) if args.risk is not None else (0.25, 0.40, 0.50),
            corr=args.corr,
            book_names=[args.book] if args.book else [DEFAULT_PRODUCTION_BOOK],
            production=production_mode,
        )


if __name__ == "__main__":
    main()
