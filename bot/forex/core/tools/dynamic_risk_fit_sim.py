"""Compare Dynamic Risk with and without fitting a lower tier into spare room.

The simulation bootstraps complete historical portfolio days from XAU M15/M30.
At most two setups are admitted per sampled day and their reserved risk may not
exceed 1.50%. This deliberately assumes the two setups overlap for the full day;
it is a conservative approximation because the reports do not retain tick-level
position overlap.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy import backtest_reporting  # noqa: E402

STREAMS = (("XAUUSD", "M15"), ("XAUUSD", "M30"))
SCENARIOS = ("holdout", "validation", "train")
TIERS = (1.00, 0.75, 0.50, 0.40)
THRESHOLDS = (0.50, 1.00, 1.50)
MAX_OPEN_RISK = 1.50
MAX_DAYS = 400


def _days(scenario: str) -> list[list[float]]:
    events: dict[object, list[tuple[pd.Timestamp, float]]] = {}
    first = last = None
    for symbol, timeframe in STREAMS:
        report = backtest_reporting.load_report(
            backtest_reporting.report_path(symbol, timeframe))
        technique = "be_after_tp1_33_33_34"
        curve = report["holdout_all_curves_r"]
        equity = np.asarray(curve["equity"][technique], dtype=float)
        values = np.diff(np.concatenate([[0.0], equity]))
        metric = report["techniques"][technique][scenario]["expectancy_r"]
        if scenario != "holdout":
            values = values - values.mean() + metric
        stamps = pd.to_datetime(curve["time"])
        first = stamps.min() if first is None else min(first, stamps.min())
        last = stamps.max() if last is None else max(last, stamps.max())
        for stamp, value in zip(stamps, values):
            events.setdefault(stamp.date(), []).append((stamp, float(value)))
    span = pd.date_range(first.normalize(), last.normalize(), freq="D")
    valid = [stamp.date() for stamp in span
             if stamp.dayofweek < 5 or stamp.date() in events]
    return [[value for _, value in sorted(events.get(day, ()))] for day in valid]


def _desired_risk(drawdown: float) -> float:
    if drawdown < THRESHOLDS[0]:
        return TIERS[0]
    if drawdown < THRESHOLDS[1]:
        return TIERS[1]
    if drawdown < THRESHOLDS[2]:
        return TIERS[2]
    return TIERS[3]


def _phase(pool: list[list[float]], target: float, fit: bool,
           rng: np.random.Generator) -> tuple[bool, int, bool]:
    equity = high_water = 0.0
    for day_number in range(1, MAX_DAYS + 1):
        sampled = pool[int(rng.integers(len(pool)))]
        reserved = daily_return = 0.0
        for result_r in sampled[:2]:
            desired = _desired_risk(max(0.0, high_water - equity))
            choices = ([tier for tier in TIERS if tier <= desired + 1e-12]
                       if fit else [desired])
            chosen = next((tier for tier in choices
                           if reserved + tier <= MAX_OPEN_RISK + 1e-12), None)
            if chosen is None:
                continue
            reserved += chosen
            daily_return += result_r * chosen
        equity += daily_return
        high_water = max(high_water, equity)
        if daily_return <= -5.0 or equity <= -10.0:
            return False, day_number, True
        if equity >= target:
            return True, max(day_number, 4), False
    return False, MAX_DAYS, False


def simulate(pool: list[list[float]], paths: int, fit: bool, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    totals, step1_days = [], []
    breaches = 0
    for _ in range(paths):
        passed1, days1, breached = _phase(pool, 10.0, fit, rng)
        breaches += int(breached)
        if not passed1:
            continue
        step1_days.append(days1)
        passed2, days2, _ = _phase(pool, 5.0, fit, rng)
        if passed2:
            totals.append(days1 + days2)
    percentile = lambda values, q: int(np.quantile(values, q)) if values else -1
    return {
        "step1": len(step1_days) / paths,
        "breach": breaches / paths,
        "two_step": len(totals) / paths,
        "p1_med": percentile(step1_days, .5),
        "p1_p90": percentile(step1_days, .9),
        "total_med": percentile(totals, .5),
        "total_p90": percentile(totals, .9),
    }


def plot(results: dict[tuple[str, bool], dict], paths: int) -> Path:
    labels = ("Holdout / current", "Validation / base", "Older / flat")
    scenarios = SCENARIOS
    before = np.array([results[(scenario, False)]["total_med"]
                       for scenario in scenarios])
    after = np.array([results[(scenario, True)]["total_med"]
                      for scenario in scenarios])
    before_p90 = np.array([results[(scenario, False)]["total_p90"]
                           for scenario in scenarios])
    after_p90 = np.array([results[(scenario, True)]["total_p90"]
                          for scenario in scenarios])
    y = np.arange(len(labels))
    fig, axis = plt.subplots(figsize=(12, 6.4), facecolor="#0b0f14")
    axis.set_facecolor("#0b0f14")
    axis.barh(y - .18, before, height=.30, color="#d97745", label="Before: block")
    axis.barh(y + .18, after, height=.30, color="#2aa879", label="After: fit tier")
    axis.errorbar(before, y - .18, xerr=[np.zeros(3), before_p90 - before],
                  fmt="none", ecolor="#f0a176", capsize=5, linewidth=1.8)
    axis.errorbar(after, y + .18, xerr=[np.zeros(3), after_p90 - after],
                  fmt="none", ecolor="#62c99f", capsize=5, linewidth=1.8)
    for index, value in enumerate(before):
        axis.text(value + 4, index - .18, f"{value}  (P90 {before_p90[index]})",
                  va="center", color="#f2f5f7", fontsize=10)
    for index, value in enumerate(after):
        axis.text(value + 4, index + .18, f"{value}  (P90 {after_p90[index]})",
                  va="center", color="#f2f5f7", fontsize=10)
    axis.set_yticks(y, labels)
    axis.invert_yaxis()
    axis.set_xlabel("Trading days for P1 + P2", color="#dbe3ea")
    axis.set_title("Dynamic Risk Fit Remaining — pass-time forecast",
                   color="#f2f5f7", loc="left", fontsize=17, pad=18)
    axis.text(0, 1.01, f"Median bars; whiskers end at P90 · Monte Carlo {paths:,} paths",
              transform=axis.transAxes, color="#9aa8b4", fontsize=10)
    axis.grid(axis="x", color="#2a3441", alpha=.7)
    axis.set_axisbelow(True)
    axis.tick_params(colors="#cbd5dd")
    for spine in axis.spines.values():
        spine.set_color("#2a3441")
    legend = axis.legend(frameon=False, loc="lower right")
    for text in legend.get_texts():
        text.set_color("#dbe3ea")
    fig.text(.01, .01, "Projection, not a guarantee. Risk caps remain 1.50%.",
             color="#9aa8b4", fontsize=9)
    fig.tight_layout(rect=(0, .04, 1, 1))
    destination = ROOT / "outputs" / "charts" / "dynamic_risk_fit_forecast.png"
    destination.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(destination, dpi=180, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nsim", type=int, default=20_000)
    parser.add_argument("--plot", action="store_true")
    args = parser.parse_args()
    if args.nsim < 100:
        parser.error("--nsim must be at least 100")
    print("scenario   mode       step1  breach  2-step   P1 med/p90  total med/p90")
    results = {}
    for scenario in SCENARIOS:
        pool = _days(scenario)
        for fit in (False, True):
            result = simulate(pool, args.nsim, fit, seed=41)
            results[(scenario, fit)] = result
            print(f"{scenario:10s} {'fit' if fit else 'block':10s} "
                  f"{result['step1']:6.1%} {result['breach']:7.1%} "
                  f"{result['two_step']:7.1%} "
                  f"{result['p1_med']:4d}/{result['p1_p90']:<4d} "
                  f"{result['total_med']:7d}/{result['total_p90']:<4d}")
    if args.plot:
        print(f"plot={plot(results, args.nsim)}")


if __name__ == "__main__":
    main()
