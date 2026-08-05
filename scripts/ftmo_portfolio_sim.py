"""Portfolio-level FTMO simulation on top of the technique-lab holdout curves.

The technique lab measures one symbol/timeframe at a time in R units. FTMO is
scored on a single account: daily loss, total loss and profit target all apply to
the combined equity. This script joins the per-stream trade series into portfolio
trading days, applies the FTMO 2-Step rules, and reports pass rates plus the
number of trading days a pass takes.

Three calibrations are reported. Each keeps the holdout day-to-day *shape* and
re-bases the per-trade mean to the expectancy measured on that split, so the
pessimistic case answers "what if the recent regime disappears".

    python scripts/ftmo_portfolio_sim.py
    python scripts/ftmo_portfolio_sim.py --by-year
    python scripts/ftmo_portfolio_sim.py --plot

Every simulation resamples the same holdout trades, so it quantifies path risk
given the edge is real. It cannot validate the edge itself.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau import backtest_reporting, config  # noqa: E402

NSIM, MAXDAYS = 20_000, 400
TECHNIQUE_OVERRIDE: str | None = None
DAILY_LIMIT, MAX_LIMIT = 5.0, 10.0          # FTMO 2-Step, percent of initial balance
STEP_TARGETS = (10.0, 5.0)

# stream -> (symbol, timeframe, extra cost R/trade on top of the lab's own model).
#
# The extra used to carry 0.02-0.08R of commission and slippage, because the lab
# charged spread alone. `technique_lab._cost_r` now models all three from
# `config.COSTS`, so anything here would be charged twice: it is 0.0 and stays 0.0
# unless there is a cost the model genuinely does not cover.
#
# The technique is no longer pinned either. It was hardcoded to BE + 33/33/34,
# which stopped being the validation-selected exit for gold once selection moved
# off the holdout — so the simulation was pricing a book nobody would trade.
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
PLOT_RISK = 0.40


def load_stream(key: str, scenario: str) -> tuple[np.ndarray, float, float]:
    """Daily R totals over the stream's own holdout span, including flat days."""
    symbol, timeframe, cost = STREAMS[key]
    report = backtest_reporting.load_report(
        backtest_reporting.report_path(symbol, timeframe))
    technique = TECHNIQUE_OVERRIDE or backtest_reporting.select_technique(report)
    curve = report["holdout_all_curves_r"]
    equity = np.asarray(curve["equity"][technique], dtype=float)
    trade_r = np.diff(np.concatenate([[0.0], equity]))
    metrics = report["techniques"][technique]
    if scenario != "holdout":
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
    return days.to_numpy(), len(series) / len(days), metrics[scenario]["expectancy_r"] - cost


def portfolio_days(pools, risk_pct: float, corr: float, rng) -> np.ndarray:
    """(NSIM, MAXDAYS) matrix of portfolio daily percent returns."""
    shape = (NSIM, MAXDAYS)
    if corr <= 0:
        total = sum(rng.choice(pool[0], size=shape) for pool in pools)
    else:
        # Blend an independent draw with a shared-quantile draw so every stream
        # has its good and bad days at the same time in the correlated part.
        quantile = rng.random(shape)
        total = sum((1 - corr) * rng.choice(pool[0], size=shape)
                    + corr * np.quantile(pool[0], quantile) for pool in pools)
    return total * risk_pct


def resolve_phase(days_pct: np.ndarray, target: float):
    """-> (passed mask, trading day the target was hit, breached mask)."""
    cum = np.cumsum(days_pct, axis=1)
    never = days_pct.shape[1] + 10
    first = lambda mask: np.where(mask.any(1), mask.argmax(1), never)
    hit = first(cum >= target)
    blown = first(cum <= -MAX_LIMIT)
    daily = first(days_pct <= -DAILY_LIMIT)
    breach = np.minimum(blown, daily)
    return hit < breach, hit + 1, breach < np.minimum(hit, never)


def simulate(keys: list[str], scenario: str, risk_pct: float, corr: float, seed: int = 41) -> dict:
    rng = np.random.default_rng(seed)
    pools = [load_stream(key, scenario) for key in keys]
    step1 = portfolio_days(pools, risk_pct, corr, rng)
    passed1, day1, failed1 = resolve_phase(step1, STEP_TARGETS[0])
    # Verification is a fresh account, so draw an independent set of paths.
    passed2, day2, _ = resolve_phase(portfolio_days(pools, risk_pct, corr, rng), STEP_TARGETS[1])
    both = passed1 & passed2
    total = np.where(both, day1 + day2, 0)
    weights = [pool[1] for pool in pools]
    pick = lambda values, q: int(np.quantile(values, q)) if len(values) else -1
    return {
        "trades_per_day": sum(weights),
        "expectancy_r": float(np.average([pool[2] for pool in pools], weights=weights)),
        "r_per_day": sum(pool[0].mean() for pool in pools),
        "step1": passed1.mean(), "breach": failed1.mean(), "two_step": both.mean(),
        "d1_med": pick(day1[passed1], .5), "d1_p90": pick(day1[passed1], .9),
        "total_med": pick(total[both], .5), "total_p90": pick(total[both], .9),
        "worst_day": float(np.quantile(step1.min(1), .05)),
    }


def report_books(risks=(0.25, 0.40, 0.50), corr: float = 0.3,
                 book_names: list[str] | None = None) -> None:
    selected_books = book_names or list(BOOKS)
    for scenario, label in SCENARIOS:
        print(f"\n{'=' * 118}\n{label}   [FTMO 2-Step: {STEP_TARGETS[0]:.0f}% then "
              f"{STEP_TARGETS[1]:.0f}%, daily {DAILY_LIMIT:.0f}%, max {MAX_LIMIT:.0f}%, "
              f"corr={corr}]\n{'=' * 118}")
        print(f"{'book':24s} {'risk':>5s} {'trd/d':>6s} {'expR':>7s} {'R/day':>7s} "
              f"{'step1':>7s} {'breach':>7s} {'2-step':>7s} {'days step1':>12s} "
              f"{'days total':>12s} {'worst day':>10s}")
        for name in selected_books:
            keys = BOOKS[name]
            for risk in risks:
                m = simulate(keys, scenario, risk, corr)
                print(f"{name:24s} {risk:5.2f} {m['trades_per_day']:6.1f} "
                      f"{m['expectancy_r']:+7.3f} {m['r_per_day']:+7.3f} {m['step1']:7.1%} "
                      f"{m['breach']:7.1%} {m['two_step']:7.1%} "
                      f"{f'{m['d1_med']}/{m['d1_p90']}':>12s} "
                      f"{f'{m['total_med']}/{m['total_p90']}':>12s} {m['worst_day']:+10.2f}%")


def report_by_year(targets=(("XAUUSD", "H1"), ("XAUUSD", "M30"),
                            ("EURUSD", "M30"))) -> None:
    """Re-run the engine and split expectancy by calendar year to expose regime risk.

    Uses the same exit the report selected on validation, and the same cost model,
    so a year here is comparable with a split there. It used to assume
    BE + 33/33/34 and add its own commission estimate on top of the lab's.
    """
    from xau import quantum, technique_lab as lab

    for symbol, timeframe in targets:
        report = backtest_reporting.load_report(
            backtest_reporting.report_path(symbol, timeframe))
        technique = TECHNIQUE_OVERRIDE or backtest_reporting.select_technique(report)
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
    fig.text(.5, -.03, f"risk {PLOT_RISK:.2f}%/trade  ·  corr {corr}  ·  "
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
    axis.text(0, -14, f"risk {PLOT_RISK:.2f}%/trade · corr {corr} · commission and slippage "
                      f"deducted · 20,000 simulated paths per bar",
              color=MUTED, fontsize=10, transform=axis.get_yaxis_transform())
    save_figure(fig, "pass_rate.png")


def plot_expectancy_by_year(streams=(("XAU H1", "XAUUSD H1"), ("XAU M30", "XAUUSD M30"),
                                     ("EUR M30", "EURUSD M30"))) -> None:
    """Per-year expectancy — the regime-dependence evidence."""
    from xau import quantum, technique_lab as lab

    fig, axes = plt.subplots(1, 3, figsize=(16, 5.4), constrained_layout=True)
    for axis, (key, title) in zip(axes, streams):
        symbol, timeframe, cost = STREAMS[key]
        report = backtest_reporting.load_report(
            backtest_reporting.report_path(symbol, timeframe))
        technique = TECHNIQUE_OVERRIDE or backtest_reporting.select_technique(report)
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
        paths = np.cumsum(portfolio_days(pools, PLOT_RISK, corr, rng)[:, :horizon], axis=1)
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
    fig.suptitle(f"Simulated equity paths · {book} at {PLOT_RISK:.2f}% risk/trade  "
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
        technique = TECHNIQUE_OVERRIDE or backtest_reporting.select_technique(report)
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
    print(f"plot_set_complete dir={PLOT_DIR} risk={PLOT_RISK:.2f}%")


def main() -> None:
    global NSIM, TECHNIQUE_OVERRIDE

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
                        help="simulate one risk percentage instead of 0.25/0.40/0.50")
    parser.add_argument("--nsim", type=int, default=NSIM,
                        help=f"Monte Carlo paths (default {NSIM:,})")
    parser.add_argument(
        "--technique",
        choices=("selected", "fixed_tp3", "be_after_tp1_33_33_34"),
        default="selected",
        help="exit policy; use be_after_tp1_33_33_34 for the >=$30K bot tier",
    )
    args = parser.parse_args()
    if args.nsim < 100:
        parser.error("--nsim must be at least 100")
    if args.risk is not None and args.risk <= 0:
        parser.error("--risk must be positive")
    NSIM = args.nsim
    TECHNIQUE_OVERRIDE = None if args.technique == "selected" else args.technique
    if args.plot:
        plot_all(args.corr)
    elif args.by_year:
        report_by_year()
    else:
        report_books(
            risks=(args.risk,) if args.risk is not None else (0.25, 0.40, 0.50),
            corr=args.corr,
            book_names=[args.book] if args.book else None,
        )


if __name__ == "__main__":
    main()
