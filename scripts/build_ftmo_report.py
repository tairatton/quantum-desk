"""Build a reproducible Markdown summary from technique-lab JSON reports."""
from __future__ import annotations

from datetime import date
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau import backtest_reporting, config  # noqa: E402


OUTPUT = config.DOCS_DIR / "FTMO_BACKTEST_SUMMARY.md"
SPLITS = ("train", "validation", "holdout")
PRODUCTION_TECHNIQUE = "be_after_tp1_33_33_34"
PRODUCTION_STREAMS = (("XAUUSD", "M15"), ("XAUUSD", "M30"))


def number(value, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.{digits}f}"


def signed(value, suffix: str = "R") -> str:
    return f"{float(value):+.2f}{suffix}"


def technique_label(key: str) -> str:
    return backtest_reporting.TECHNIQUE_NAMES.get(key, key)


def production_reports() -> list[dict]:
    return [
        backtest_reporting.load_report(
            backtest_reporting.report_path(symbol, timeframe)
        )
        for symbol, timeframe in PRODUCTION_STREAMS
    ]


def production_table(reports: list[dict]) -> list[str]:
    lines = [
        "| Symbol | TF | Exit technique | Holdout trades | Win rate | Net | Expectancy | PF | Max DD |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for report in reports:
        row = report["techniques"][PRODUCTION_TECHNIQUE]["holdout"]
        lines.append(
            f"| {report['symbol']} | {report['timeframe']} | "
            f"{technique_label(PRODUCTION_TECHNIQUE)} | {row['trades']} "
            f"| {number(row['win_rate'])}% | {signed(row['net_r'])} "
            f"| {signed(row['expectancy_r'])}/trade | {number(row['profit_factor'])} "
            f"| {number(row['max_drawdown_r'])}R |"
        )
    return lines


def production_split_table(reports: list[dict]) -> list[str]:
    lines = [
        "| Symbol | TF | Split | Trades | Win rate | Net | Expectancy | PF | Max DD |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for report in reports:
        metrics = report["techniques"][PRODUCTION_TECHNIQUE]
        for split in SPLITS:
            row = metrics[split]
            lines.append(
                f"| {report['symbol']} | {report['timeframe']} | {split.title()} "
                f"| {row['trades']} | {number(row['win_rate'])}% | {signed(row['net_r'])} "
                f"| {signed(row['expectancy_r'])}/trade | {number(row['profit_factor'])} "
                f"| {number(row['max_drawdown_r'])}R |"
            )
    return lines


def selected_table(reports: list[dict]) -> list[str]:
    lines = [
        "| Symbol | TF | Exit technique | Holdout trades | Win rate | Net | PF | Max DD | Consistent? |",
        "|---|---:|---|---:|---:|---:|---:|---:|---|",
    ]
    for report in reports:
        best = backtest_reporting.best_holdout(report)
        consistent = "Yes" if backtest_reporting.is_consistent(report, best["technique"]) else "No"
        lines.append(
            f"| {report['symbol']} | {report['timeframe']} | {technique_label(best['technique'])} "
            f"| {best['trades']} | {number(best['win_rate'])}% | {signed(best['net_r'])} "
            f"| {number(best['profit_factor'])} | {number(best['max_drawdown_r'])}R | {consistent} |"
        )
    return lines


def rejected_table() -> list[str]:
    """Name what was dropped, so an absence is never mistaken for an oversight."""
    if not backtest_reporting.REJECTED:
        return []
    lines = ["### Considered and rejected", "",
             "| Symbol | TF | Why |", "|---|---:|---|"]
    for symbol, (timeframe, reason) in backtest_reporting.REJECTED.items():
        lines.append(f"| {symbol} | {timeframe} | {reason} |")
    return lines


def split_table(report: dict) -> list[str]:
    best = backtest_reporting.best_holdout(report)
    metrics = backtest_reporting.split_metrics(report, best["technique"])
    lines = [
        f"### {report['symbol']} {report['timeframe']}",
        "",
        f"Selected exit: **{technique_label(best['technique'])}**",
        "",
        "| Split | Trades | Win rate | Net | Expectancy | PF | Max DD |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for split in SPLITS:
        row = metrics[split]
        lines.append(
            f"| {split.title()} | {row['trades']} | {number(row['win_rate'])}% "
            f"| {signed(row['net_r'])} | {signed(row['expectancy_r'])}/trade "
            f"| {number(row['profit_factor'])} | {number(row['max_drawdown_r'])}R |"
        )
    chart_root = f"../outputs/charts/ftmo/symbols/{report['symbol']}/{report['timeframe']}"
    lines.extend([
        "",
        f"[FTMO performance chart]({chart_root}/performance.png)",
        "",
    ])
    return lines


def all_timeframes_table(reports: list[dict]) -> list[str]:
    lines = [
        "| Symbol | TF | Bars | Best exit | Holdout trades | Win rate | Net | PF | Max DD |",
        "|---|---:|---:|---|---:|---:|---:|---:|---:|",
    ]
    for report in reports:
        best = backtest_reporting.best_holdout(report)
        lines.append(
            f"| {report['symbol']} | {report['timeframe']} | {report['bars']} "
            f"| {technique_label(best['technique'])} | {best['trades']} "
            f"| {number(best['win_rate'])}% | {signed(best['net_r'])} "
            f"| {number(best['profit_factor'])} | {number(best['max_drawdown_r'])}R |"
        )
    return lines


def build() -> str:
    selected = backtest_reporting.sort_reports(backtest_reporting.selected_reports())
    all_reports = backtest_reporting.sort_reports(backtest_reporting.discover_reports())
    production = production_reports()
    lines = [
        "# FTMO Technique-Lab Backtest Summary",
        "",
        f"Generated: {date.today().isoformat()}",
        "",
        "## Scope and method",
        "",
        "- Deterministic HTF Quantum entries; AI/Elliott Wave is not used.",
        "- Chronological split: 60% train, 20% validation, 20% locked holdout.",
        "- A 141-bar purge separates the earlier splits from subsequent outcomes.",
        "- **The exit technique is chosen on validation, never on the holdout.** Ranking by "
        "holdout net R let the holdout both pick the technique and score it; USDJPY M15 read "
        "+11.5R that way and -8.5R once chosen honestly.",
        "- **Cost is spread + commission + slippage** (`xau.config.COSTS`). Bars are bid-quoted, "
        "so a round trip pays the spread once; commission and slippage are absent from the feed "
        "and are modelled per symbol. The figures are estimates, not measurements - replace them "
        "with what an FTMO demo actually charges.",
        "- Every exit technique uses the same filled entries, so exit comparisons do not change trade frequency.",
        "",
        "## Production-aligned results for the current bot",
        "",
        "The live account starts at $50,000. Its `capital_tier` setting resolves to "
        "`be_after_tp1_33_33_34` for both XAUUSD M15 and M30. The tables below pin both "
        "timeframes to that exit instead of selecting an exit separately for research.",
        "",
        *production_table(production),
        "",
        "### Production-aligned split results",
        "",
        *production_split_table(production),
        "",
        "## Selected smallest practical profitable timeframe",
        "",
        *selected_table(selected),
        "",
        "The selection prioritises the smallest timeframe with a material edge and positive "
        "train, validation, and holdout splits. Tiny positive results with high drawdown are rejected. "
        "A row marked `Consistent? = No` is **not tradeable** - it is listed because it was a "
        "candidate, not because it passed.",
        "",
        *rejected_table(),
        "",
        "Charts assume 0.25% account risk per trade.",
        "",
        "[Selected overview](../outputs/charts/ftmo/summary/overview.png) · "
        "[Split expectancy](../outputs/charts/ftmo/summary/split_expectancy.png) · "
        "[Timeframe matrix](../outputs/charts/ftmo/summary/timeframe_matrix.png)",
        "",
        "## Selected split results",
        "",
    ]
    for report in selected:
        lines.extend(split_table(report))
    lines.extend([
        "## All available technique-lab results",
        "",
        *all_timeframes_table(all_reports),
        "",
        "## FTMO risk plan",
        "",
        "- Preferred evaluation: FTMO 2-Step, because its max loss is static rather than "
        "trailing and this system's worst historical drawdown is 15-22R.",
        "- **Gold only.** EURUSD was the intended half-risk helper and does not survive the "
        "cost model on any timeframe, so there is no second stream left to diversify into.",
        "- Risk per trade: 0.25-0.40% of the **initial** balance, never the live balance.",
        "- Aggregate open risk: maximum 0.80%, and no more than two positions at once, "
        "because gold M15 and M30 usually agree.",
        "- Internal daily stop: -1.50%; stop after three consecutive losing trades; stop "
        "entirely once the target and the four trading days are both in.",
        "- No martingale, grid recovery, averaging down, or widening a stop loss.",
        "- Estimated duration, not a guarantee: 25-50 trading days for the 10% Challenge and "
        "13-25 trading days for the 5% Verification. The news and market-close blackouts in "
        "`bot/` are wider than the rules require, so expect fewer trades than the study's "
        "2.8/day and a correspondingly longer run.",
        "",
        "Official rules must be checked again before purchase: "
        "[FTMO Trading Objectives](https://ftmo.com/en/trading-objectives/) and "
        "[FTMO 2-Step](https://ftmo.com/en/2-step-challenge/).",
        "",
        "## Reproduce",
        "",
        "```powershell",
        "python scripts\\plot_ftmo_charts.py",
        "python scripts\\build_ftmo_report.py",
        "python -m unittest discover -s tests -q",
        "```",
        "",
        "## Limitations",
        "",
        "Historical results do not guarantee an FTMO pass. The cost figures are estimates of the "
        "right order of magnitude, not measurements taken on the account that will be traded - "
        "measure them on an FTMO demo first, because the whole difference between an edge and no "
        "edge on the FX pairs was cost. Data comes from Exness, whose spreads and server clock "
        "both differ from FTMO's. Gold ran two losing years and one flat year out of nine, so a "
        "flat regime is a normal outcome rather than a tail. Nothing here simulates news or "
        "weekend gaps.",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    config.ensure_dirs()
    OUTPUT.write_text(build(), encoding="utf-8")
    print(f"[SAVED] {OUTPUT}")


if __name__ == "__main__":
    main()
