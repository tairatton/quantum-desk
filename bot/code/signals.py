"""Bridge from the backtested strategy to something the bot can act on.

The strategy is not reimplemented here. `xau.quantum.analyse` is the same code
the technique lab measured, so live and backtest cannot drift apart. This module
only decides *which* action the current plan implies.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from xau import quantum


@dataclass(frozen=True)
class Intent:
    action: str            # "market" | "limit" | "cancel" | "wait"
    plan_id: str
    timeframe: str
    direction: int
    entry: float
    stop: float
    risk: float
    targets: tuple[float, float, float]
    status: str
    signal_time: str
    bars_since_signal: int
    reason: str = ""

    @property
    def side(self) -> str:
        return "BUY" if self.direction == 1 else "SELL"


WAIT = "wait"


def read(frame: pd.DataFrame, timeframe: str) -> Intent | None:
    """Latest plan on closed bars, expressed as an intent.

    `frame` must contain closed bars only — `Broker.bars` already drops the bar
    still forming, which is what keeps the live signal free of look-ahead.
    """
    result = quantum.analyse(frame, timeframe)
    plan = result["plan"]
    if plan is None:
        return None

    data = result["data"]
    last_index = len(data) - 1
    signal_time = str(pd.Timestamp(data["time"].iloc[plan["signal_index"]]))
    plan_id = f"{timeframe.upper()}@{signal_time}"
    common = {
        "plan_id": plan_id, "timeframe": timeframe.upper(),
        "direction": plan["direction"], "entry": float(plan["entry"]),
        "stop": float(plan["stop"]), "risk": float(plan["risk"]),
        "targets": tuple(float(price) for price in plan["targets"]),
        "status": plan["status"], "signal_time": signal_time,
        "bars_since_signal": last_index - plan["signal_index"],
    }

    if plan["pending"]:
        return Intent(action="limit", reason="waiting for the 50% retracement", **common)
    if plan["active"]:
        fresh = plan["entry_fill_index"] == last_index
        return Intent(action="market" if fresh else WAIT,
                      reason="filled on the bar that just closed" if fresh
                             else "already running", **common)
    # Cancelled, expired, invalidated or resolved: nothing should still be working.
    return Intent(action="cancel", reason=plan["status"], **common)


def bars_since(frame: pd.DataFrame, bar_time: str) -> int:
    """How many closed bars have printed since a given bar timestamp."""
    stamps = pd.to_datetime(frame["time"])
    matches = stamps.index[stamps == pd.Timestamp(bar_time)]
    if len(matches) == 0:
        return 0
    return int(len(frame) - 1 - matches[0])


def bars_since_moment(frame: pd.DataFrame, moment) -> int:
    """How many closed bars have printed after an arbitrary instant.

    `bars_since` needs a timestamp that *is* a bar boundary and answers 0 for
    anything else, which is the right answer for a plan anchored to its fill bar
    but the wrong one for a position whose only timestamp is the moment it
    opened. Counting bars rather than wall-clock hours is what makes the two
    agree: 120 M30 bars is 60 trading hours, and a weekend is not part of it.
    """
    stamps = pd.to_datetime(frame["time"])
    return int((stamps > pd.Timestamp(moment)).sum())


TRADE_TIMEOUT_BARS = quantum.MAX_TRADE_BARS
ENTRY_TIMEOUT_BARS = quantum.ENTRY_TIMEOUT
