"""Render a compact FTMO-focused Matplotlib chart pack.

The chart pack uses the selected exit policy for each symbol, converts R to
account percentage using a configurable fixed risk per trade, and keeps
train/validation/holdout differences visible.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys

import matplotlib.colors as mcolors
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy import backtest_reporting, config  # noqa: E402


OUTPUT_DIR = config.FTMO_CHART_DIR
SUMMARY_DIR = OUTPUT_DIR / "summary"
SYMBOL_DIR = OUTPUT_DIR / "symbols"
TIMEFRAMES = ("M5", "M15", "M30", "H1", "H4")
BG = "#0b0f14"
PANEL = "#111821"
GRID = "#2a3441"
TEXT = "#e5edf5"
MUTED = "#9eabb8"
GREEN = "#35c994"
AMBER = "#f4b942"
BLUE = "#5b9cf0"
PURPLE = "#a78bfa"
RED = "#ef6a7b"


def style_axis(axis) -> None:
    axis.set_facecolor(PANEL)
    axis.grid(True, color=GRID, linewidth=.7, alpha=.75)
    axis.spines[["top", "right"]].set_visible(False)
    axis.spines[["left", "bottom"]].set_color("#586574")
    axis.tick_params(colors=MUTED)
    axis.xaxis.label.set_color(MUTED)
    axis.yaxis.label.set_color(TEXT)
    axis.title.set_color(TEXT)


def save(fig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.patch.set_facecolor(BG)
    fig.savefig(path, dpi=160, facecolor=BG, bbox_inches="tight")
    plt.close(fig)


def label(report: dict) -> str:
    return f"{report['symbol']}\n{report['timeframe']}"


def selected_data(report: dict, risk_percent: float) -> tuple[dict, np.ndarray, np.ndarray]:
    best = backtest_reporting.best_holdout(report)
    curve = report["holdout_all_curves_r"]
    equity = np.asarray(curve["equity"][best["technique"]], dtype=float) * risk_percent
    drawdown = np.asarray(curve["drawdown"][best["technique"]], dtype=float) * risk_percent
    return best, equity, drawdown


def plot_performance(report: dict, risk_percent: float) -> Path:
    best, equity, drawdown = selected_data(report, risk_percent)
    times = pd.to_datetime(report["holdout_all_curves_r"]["time"])
    consistent = backtest_reporting.is_consistent(report, best["technique"])
    line_color = GREEN if consistent else AMBER
    exit_name = backtest_reporting.TECHNIQUE_NAMES[best["technique"]]

    fig, (equity_axis, drawdown_axis) = plt.subplots(
        2, 1, figsize=(15, 9), sharex=True,
        gridspec_kw={"height_ratios": (2.2, 1)}, constrained_layout=True,
    )
    style_axis(equity_axis)
    style_axis(drawdown_axis)

    equity_axis.plot(times, equity, color=line_color, linewidth=2.2)
    equity_axis.fill_between(times, equity, 0, color=line_color, alpha=.12)
    equity_axis.axhline(0, color=MUTED, linewidth=.8)
    for target, color in ((5, PURPLE), (10, GREEN)):
        equity_axis.axhline(target, color=color, linewidth=1, linestyle="--", alpha=.8)
        equity_axis.text(times[0], target, f"  FTMO {target}% target", color=color, va="bottom")
    equity_axis.annotate(
        f"{equity[-1]:+.2f}%", xy=(times[-1], equity[-1]), xytext=(-8, 12),
        textcoords="offset points", ha="right", color=line_color, fontsize=12,
    )
    equity_axis.set_ylabel("Account return (%)")
    status = "consistent" if consistent else "provisional — negative train split"
    equity_axis.set_title(
        f"{report['symbol']} {report['timeframe']} | {exit_name} | {status}",
        loc="left", fontsize=15,
    )

    drawdown_axis.fill_between(times, drawdown, 0, color=RED, alpha=.35)
    drawdown_axis.plot(times, drawdown, color=RED, linewidth=1.5)
    drawdown_axis.axhline(0, color=MUTED, linewidth=.8)
    drawdown_axis.annotate(
        f"max {drawdown.min():.2f}%", xy=(times[np.argmin(drawdown)], drawdown.min()),
        xytext=(8, -16), textcoords="offset points", color=RED,
    )
    drawdown_axis.set_ylabel("Drawdown (%)")
    drawdown_axis.set_xlabel(f"Holdout signal time · fixed risk {risk_percent:.2f}% per trade")
    drawdown_axis.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=5, maxticks=10))
    drawdown_axis.xaxis.set_major_formatter(mdates.DateFormatter("%d %b %Y"))

    output = SYMBOL_DIR / report["symbol"] / report["timeframe"] / "performance.png"
    save(fig, output)
    return output


def plot_overview(reports: list[dict], risk_percent: float) -> Path:
    best_rows = [backtest_reporting.best_holdout(report) for report in reports]
    labels = [label(report) for report in reports]
    colors = [
        GREEN if backtest_reporting.is_consistent(report, best["technique"]) else AMBER
        for report, best in zip(reports, best_rows)
    ]
    metrics = (
        ([row["net_r"] * risk_percent for row in best_rows], "Holdout return (%)", "{:.2f}%"),
        ([row["win_rate"] for row in best_rows], "Win rate (%)", "{:.2f}%"),
        ([row["profit_factor"] or 0 for row in best_rows], "Profit factor", "{:.2f}"),
        ([row["max_drawdown_r"] * risk_percent for row in best_rows], "Max drawdown (%)", "{:.2f}%"),
    )
    x = np.arange(len(reports))
    fig, axes = plt.subplots(2, 2, figsize=(15, 9), constrained_layout=True)
    for axis, (values, ylabel, value_format) in zip(axes.flat, metrics):
        style_axis(axis)
        bars = axis.bar(x, values, color=colors, alpha=.9)
        axis.set_xticks(x, labels)
        axis.set_ylabel(ylabel)
        axis.bar_label(
            bars, labels=[value_format.format(value) for value in values],
            padding=3, color=TEXT, fontsize=9,
        )
        if ylabel == "Profit factor":
            axis.axhline(1, color=AMBER, linewidth=1)
    axes[0, 0].set_title(
        f"FTMO candidates · locked holdout · {risk_percent:.2f}% risk/trade",
        loc="left", fontsize=15,
    )
    output = SUMMARY_DIR / "overview.png"
    save(fig, output)
    return output


def plot_split_expectancy(reports: list[dict], risk_percent: float) -> Path:
    labels = [label(report) for report in reports]
    x = np.arange(len(reports))
    width = .25
    fig, axis = plt.subplots(figsize=(15, 7), constrained_layout=True)
    style_axis(axis)
    for offset, (split, color) in enumerate((
        ("train", BLUE), ("validation", PURPLE), ("holdout", GREEN),
    )):
        values = []
        for report in reports:
            technique = backtest_reporting.best_holdout(report)["technique"]
            expectancy = report["techniques"][technique][split]["expectancy_r"] or 0
            values.append(expectancy * risk_percent)
        axis.bar(x + (offset - 1) * width, values, width, label=split.title(), color=color)
    axis.axhline(0, color=MUTED, linewidth=.9)
    axis.set_xticks(x, labels)
    axis.set_ylabel("Expected account return per trade (%)")
    axis.set_title("Robustness by split · comparable expectancy per trade", loc="left", fontsize=15)
    axis.legend(frameon=False, ncol=3)
    output = SUMMARY_DIR / "split_expectancy.png"
    save(fig, output)
    return output


def matrix_data(reports: list[dict], metric: str, risk_percent: float) -> tuple[list[str], np.ndarray]:
    symbols = list(dict.fromkeys(report["symbol"] for report in reports))
    values = np.full((len(symbols), len(TIMEFRAMES)), np.nan)
    for report in reports:
        if report["timeframe"] not in TIMEFRAMES:
            continue
        row = symbols.index(report["symbol"])
        column = TIMEFRAMES.index(report["timeframe"])
        best = backtest_reporting.best_holdout(report)
        value = best[metric]
        values[row, column] = value * risk_percent if metric == "net_r" else value
    return symbols, values


def plot_timeframe_matrix(reports: list[dict], risk_percent: float) -> Path:
    symbols, returns = matrix_data(reports, "net_r", risk_percent)
    _, profit_factors = matrix_data(reports, "profit_factor", risk_percent)
    fig, axes = plt.subplots(1, 2, figsize=(15, 7), constrained_layout=True)
    panels = (
        (axes[0], returns, "Holdout return (%)", "{:+.2f}%", 0.0),
        (axes[1], profit_factors, "Profit factor", "{:.2f}", 1.0),
    )
    for axis, data, title, value_format, center in panels:
        finite = data[np.isfinite(data)]
        low = min(float(finite.min()), center - .01)
        high = max(float(finite.max()), center + .01)
        norm = mcolors.TwoSlopeNorm(vmin=low, vcenter=center, vmax=high)
        image = axis.imshow(data, cmap="RdYlGn", norm=norm, aspect="auto")
        axis.set_facecolor(PANEL)
        axis.set_xticks(np.arange(len(TIMEFRAMES)), TIMEFRAMES)
        axis.set_yticks(np.arange(len(symbols)), symbols)
        axis.tick_params(colors=TEXT)
        axis.set_title(title, color=TEXT, loc="left", fontsize=14)
        for row in range(data.shape[0]):
            for column in range(data.shape[1]):
                value = data[row, column]
                text = "—" if np.isnan(value) else value_format.format(value)
                text_color = MUTED if np.isnan(value) else "#10151b"
                axis.text(column, row, text, ha="center", va="center", color=text_color, fontsize=10)
        colorbar = fig.colorbar(image, ax=axis, shrink=.8)
        colorbar.ax.tick_params(colors=MUTED)
    fig.suptitle("Best exit policy by available symbol and timeframe", color=TEXT, fontsize=16)
    output = SUMMARY_DIR / "timeframe_matrix.png"
    save(fig, output)
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--risk-percent", type=float, default=.25,
        help="fixed account percentage risked per trade (default: 0.25)",
    )
    args = parser.parse_args()
    if not 0 < args.risk_percent <= 1:
        parser.error("--risk-percent must be greater than 0 and at most 1")
    return args


def main() -> None:
    args = parse_args()
    plt.style.use("dark_background")
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    selected = backtest_reporting.sort_reports(backtest_reporting.selected_reports())
    all_reports = backtest_reporting.sort_reports(backtest_reporting.discover_reports())
    outputs = [plot_performance(report, args.risk_percent) for report in selected]
    outputs.extend((
        plot_overview(selected, args.risk_percent),
        plot_split_expectancy(selected, args.risk_percent),
        plot_timeframe_matrix(all_reports, args.risk_percent),
    ))
    for output in outputs:
        print(f"[PLOTTED] {output}")
    print(f"plot_set_complete files={len(outputs)} risk={args.risk_percent:.2f}%")


if __name__ == "__main__":
    main()
