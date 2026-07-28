"""Shared helpers for deterministic technique-lab reports.

This module keeps report discovery, naming, and the selected FTMO candidates in
one place so plotting and Markdown generation cannot drift apart.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from . import config


TECHNIQUE_NAMES = {
    "fixed_tp1": "Full TP1",
    "fixed_tp2": "Full TP2",
    "fixed_tp3": "Full TP3",
    "scale_50_50_tp1_tp2": "Scale 50/50 TP1-TP2",
    "scale_50_25_25": "Scale 50/25/25",
    "scale_33_33_34": "Scale 33/33/34",
    "be_after_tp1_50_25_25": "BE + 50/25/25",
    "be_after_tp1_33_33_34": "BE + 33/33/34",
    "regime_adaptive": "Regime adaptive",
}

# What is actually traded, after selecting each technique on validation rather
# than on the holdout that then scores it. The earlier list came from the biased
# selection and named five symbols; three of them do not survive it:
#
#   USDJPY M15  +11.5R -> -8.5R   the whole result was the selection
#   GBPUSD      thin at M15/M30, negative at H1/H4
#   BTCUSD M5   holds up (+29.6R) but on 5 weeks of data, and cost is 5-15% of R
#   XAUUSD M5   holds up, but 7 weeks of data is too little to size an account on
#
# See docs/FTMO_SYMBOL_AND_TIMELINE.md. Gold M30 is the core because it has four
# years covering a flat regime; EURUSD M30 is a half-risk helper.
SELECTED_TIMEFRAMES = {
    "XAUUSD": "M30",
    "EURUSD": "M30",
}

# Kept for the record so the report can still show what was dropped and why,
# rather than the reader having to notice an absence.
REJECTED = {
    "USDJPY": ("M15", "holdout flips to -8.5R once the technique is picked on validation"),
    "GBPUSD": ("M15", "edge too thin to carry cost: 10.6% of R is spread alone"),
    "BTCUSD": ("M5", "only 5 weeks of holdout, and cost is 5-15% of R"),
}


def report_path(symbol: str, timeframe: str) -> Path:
    return config.TECHNIQUE_REPORT_DIR / symbol.upper() / timeframe.upper() / "report.json"


def load_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def selected_reports() -> list[dict]:
    reports = []
    missing = []
    for symbol, timeframe in SELECTED_TIMEFRAMES.items():
        path = report_path(symbol, timeframe)
        if not path.exists():
            missing.append(str(path))
        else:
            reports.append(load_report(path))
    if missing:
        raise FileNotFoundError("Missing selected technique-lab reports:\n" + "\n".join(missing))
    return reports


def discover_reports() -> list[dict]:
    paths = sorted(config.TECHNIQUE_REPORT_DIR.glob("*/*/report.json"))
    return [load_report(path) for path in paths]


def best_holdout(report: dict) -> dict:
    """The holdout result of the technique chosen on *validation*.

    This used to return `holdout_ranking[0]`, which is ranked by holdout net R —
    so the holdout picked the technique and then scored it, and the number
    reported as out-of-sample had already been used to make a decision. Under
    that method USDJPY M15 read +11.5R; selecting on validation and only then
    reading the holdout gives -8.5R. The edge was the selection.

    Validation is the last split allowed to influence a choice, so the technique
    is taken from there and the holdout is left to report what it reports.
    """
    technique = select_technique(report)
    for row in report["holdout_ranking"]:
        if row["technique"] == technique:
            return row
    return report["holdout_ranking"][0]


def select_technique(report: dict) -> str:
    """Highest validation net R. Ties break on the lower validation drawdown.

    Drawdown is read defensively: a report written before it was recorded should
    still yield a choice rather than raise, since the net R alone decides all but
    exact ties.
    """
    techniques = report["techniques"]

    def rank(name: str) -> tuple:
        split = techniques[name].get("validation", {})
        return (-float(split.get("net_r") or 0.0),
                float(split.get("max_drawdown_r") or 0.0),
                name)

    return min(techniques, key=rank)


def holdout_ranked_by_holdout(report: dict) -> dict:
    """The old, optimistic answer — kept so the gap can be shown, not hidden."""
    return report["holdout_ranking"][0]


def selection_gap(report: dict) -> float:
    """Holdout R lost when the technique is chosen honestly, in R.

    A large positive number means most of the apparent edge came from picking the
    technique with the holdout that then scored it.
    """
    return (float(holdout_ranked_by_holdout(report)["net_r"])
            - float(best_holdout(report)["net_r"]))


def split_metrics(report: dict, technique: str) -> dict:
    return report["techniques"][technique]


def is_consistent(report: dict, technique: str | None = None) -> bool:
    chosen = technique or best_holdout(report)["technique"]
    metrics = split_metrics(report, chosen)
    return all(float(metrics[split]["net_r"]) > 0 for split in ("train", "validation", "holdout"))


def sort_reports(reports: Iterable[dict]) -> list[dict]:
    symbol_order = {symbol: index for index, symbol in enumerate(config.SYMBOLS)}
    timeframe_order = {timeframe: index for index, timeframe in enumerate(config.WEB_TIMEFRAMES)}
    return sorted(
        reports,
        key=lambda report: (
            symbol_order.get(report["symbol"], 999),
            timeframe_order.get(report["timeframe"], 999),
        ),
    )
