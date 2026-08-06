"""Append-only event log.

Its job is to make live results comparable with the backtest: every entry
records the plan's R distance, so realised profit can be divided by risk_cash to
get an R outcome and stacked against `test/forex/outputs/backtests/technique_lab`. Without
that the bot could drift for weeks before anyone noticed.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

_ENABLED = True


def set_enabled(enabled: bool) -> bool:
    """Enable/disable append writes and return the previous setting.

    Dry-run lifecycle checks may replay closed deals in memory. They must not
    contaminate the live evidence log with duplicate trade outcomes.
    """
    global _ENABLED
    previous = _ENABLED
    _ENABLED = bool(enabled)
    return previous


def write(path: Path, event: str, **payload) -> dict:
    record = {"at": datetime.now().isoformat(timespec="seconds"), "event": event, **payload}
    if not _ENABLED:
        return record
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return record


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    records = []
    for index, line in enumerate(lines):
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            # Append-only logs can end with half a JSON object if power is lost
            # during the final write. Ignore only that final torn record; an
            # invalid interior row is genuine corruption and must still fail.
            if index == len(lines) - 1:
                continue
            raise
    return records


def summarise(path: Path) -> dict:
    """Live R statistics, in the same shape the technique lab reports."""
    closes = [record for record in read(path) if record["event"] == "trade_closed"]
    values = [record["r"] for record in closes if record.get("r") is not None]
    if not values:
        return {"trades": 0}
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_loss = -sum(losses)
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": len(values), "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(values) * 100, 2),
        "net_r": round(sum(values), 3),
        "expectancy_r": round(sum(values) / len(values), 4),
        "profit_factor": round(sum(wins) / gross_loss, 3) if gross_loss else None,
        "max_drawdown_r": round(drawdown, 3),
    }
