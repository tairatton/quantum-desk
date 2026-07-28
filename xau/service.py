"""Analysis service layer shared by the CLI and the web server.

Two problems this solves that a bare call into `mt5_source` does not:

1. **MT5 is a process-wide singleton.** `initialize()` / `shutdown()` mutate
   global state inside the terminal's IPC channel. Two Flask worker threads
   calling it at once will interleave and corrupt each other, so every access
   is serialised behind one lock.

2. **Clicking between timeframes should not hammer the terminal.** Results are
   cached with a TTL derived from the bar size - a 5-minute chart may refetch
   after 75s, a 4-hour chart after 5 minutes.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

import pandas as pd

from . import config, indicators, mt5_source, quantum

# Every MT5 call in the process goes through this.
_MT5_LOCK = threading.RLock()

_CACHE: dict[tuple, "_Entry"] = {}
_CACHE_LOCK = threading.Lock()

MIN_TTL, MAX_TTL = 30.0, 300.0

# Floor on how often a forced refresh may actually reach MT5.
MIN_REFETCH = 20.0

# Live quotes are cheap but not free; this keeps them fresh without spamming.
TICK_TTL = 5.0


@dataclass
class _Entry:
    value: Any
    expires_at: float


def _ttl_for(timeframe: str) -> float:
    """Refresh at roughly a quarter of the bar duration, within sane bounds."""
    secs = config.TIMEFRAME_SECONDS.get(timeframe.upper(), 3600)
    return max(MIN_TTL, min(MAX_TTL, secs / 4))


def _cached(key: tuple, ttl: float, producer):
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None and hit.expires_at > now:
            return hit.value, True

    # Produced outside the cache lock so a slow MT5 fetch never blocks readers.
    value = producer()
    with _CACHE_LOCK:
        _CACHE[key] = _Entry(value, time.monotonic() + ttl)
    return value, False


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------
def get_bars(
    symbol: str, timeframe: str, bars: int = config.DEFAULT_BARS,
    refresh: bool = False,
) -> tuple[pd.DataFrame, mt5_source.DataMeta | None, bool]:
    """Fetch bars through the cache. Returns (df, meta, was_cached)."""
    timeframe = timeframe.upper()
    symbol = symbol.upper()
    key = ("bars", symbol, timeframe, bars)

    if refresh:
        # A page polling once a minute - times however many tabs are open -
        # must not turn into that many MT5 round trips. Honour the refresh only
        # if the last real fetch is older than MIN_REFETCH.
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            age = None if hit is None else (
                hit.expires_at - _ttl_for(timeframe) - time.monotonic()
            )
            if hit is None or age is None or -age >= MIN_REFETCH:
                _CACHE.pop(key, None)

    def produce():
        try:
            with _MT5_LOCK:
                df, meta = mt5_source.fetch(symbol=symbol, timeframe=timeframe, bars=bars)
                mt5_source.save(df, meta)
                # `save` merges the fresh window into the deeper archive.
                # Analyse that merged history so Pine counters do not jump
                # between 1,500-bar live refreshes and 6,000-bar CLI runs.
                df, meta = mt5_source.load(symbol, timeframe)
            return df, meta
        except mt5_source.MT5Error:
            # A dashboard polling every minute must survive a terminal that
            # hiccups. Serve the last saved bars and let the caller flag them
            # as stale, rather than taking the whole page down.
            if mt5_source.csv_path(symbol, timeframe).exists():
                df, meta = mt5_source.load(symbol, timeframe)
                print(f"[WARN] MT5 unavailable - serving cached {symbol} {timeframe}")
                return df, meta
            raise

    (df, meta), was_cached = _cached(key, _ttl_for(timeframe), produce)
    return df, meta, was_cached


def get_tick(symbol: str) -> dict | None:
    """Live quote, cached briefly so rapid polling stays cheap."""
    def produce():
        try:
            with _MT5_LOCK:
                return mt5_source.latest_tick(symbol)
        except mt5_source.MT5Error:
            return None

    value, _ = _cached(("tick", symbol.upper()), TICK_TTL, produce)
    return value


def health() -> dict:
    """Terminal / account status for the UI banner."""
    try:
        with _MT5_LOCK:
            with mt5_source.connection() as mt:
                info = mt.account_info()
                term = mt.terminal_info()
                return {
                    "ok": True,
                    "account": info.login if info else None,
                    "server": info.server if info else None,
                    "connected": bool(term.connected) if term else False,
                }
    except mt5_source.MT5Error as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def analyse(
    symbol: str = config.DEFAULT_SYMBOL,
    timeframe: str = config.DEFAULT_TIMEFRAME,
    bars: int = config.DEFAULT_BARS,
    bars_shown: int = config.DEFAULT_PLOT_BARS,
    refresh: bool = False,
) -> dict:
    """Full Pine HTF Quantum bundle for one symbol + timeframe."""
    timeframe = timeframe.upper()
    symbol = symbol.upper()
    if timeframe not in config.TIMEFRAMES:
        raise ValueError(f"Unknown timeframe '{timeframe}'")
    config.symbol_cfg(symbol)  # raises on unknown symbol

    df, meta, was_cached = get_bars(symbol, timeframe, bars, refresh=refresh)
    window = df.tail(bars_shown).reset_index(drop=True)

    key = ("quantum", symbol, timeframe,
           len(df), str(df["time"].iloc[-1]))

    def produce():
        return quantum.analyse(df, timeframe)

    quantum_result, _ = _cached(key, _ttl_for(timeframe), produce)
    bias = indicators.trend_bias(window)

    return {
        "symbol": symbol,
        "broker_symbol": meta.symbol if meta else symbol,
        "timeframe": timeframe,
        "tick": get_tick(symbol),
        "meta": meta,
        "cached": was_cached,
        "df": window,
        "bias": bias,
        "quantum": quantum_result,
    }


# ---------------------------------------------------------------------------
# JSON serialisation for the web layer
# ---------------------------------------------------------------------------
def to_json(result: dict) -> dict:
    """Shape a Pine-only Quantum result for the browser."""
    bias = result["bias"]
    meta = result["meta"]
    q = quantum.public(result["quantum"])
    counts, rates = q["counts"], q["rates"]
    resolved_tp1 = counts["wins"][0] + counts["losses"][0]
    stats = {
        "trades": counts["filled"], "win_rate": rates["TP1"],
        "tp_win_rates": rates,
        "tp_hits_by_target": {f"TP{i+1}": counts["wins"][i] for i in range(3)},
        "tp_hits": counts["wins"][0], "sl_hits": counts["losses"][0],
        "timeouts": counts["timed_out"], "resolved_tp1": resolved_tp1,
        "expectancy_r": None, "t_stat": None, "significant": False,
    }

    return {
        "timeframe": result["timeframe"],
        "symbol": result["symbol"],
        "broker_symbol": result.get("broker_symbol"),
        "label": config.symbol_cfg(result["symbol"])["label"],
        "tick": result.get("tick"),
        "live": result.get("tick") is not None,
        "served_at": pd.Timestamp.utcnow().strftime("%H:%M:%S"),
        "setup": q["setup"],
        "quantum": q,
        "stats": stats,
        "cached": result["cached"],
        "source": {
            "kind": meta.source if meta else "unknown",
            "is_real": meta.is_real if meta else False,
            "account": meta.account if meta else None,
            "server": meta.server if meta else None,
            "fetched_at": meta.fetched_at if meta else None,
        },
        "bias": {
            "time": str(pd.Timestamp(bias["time"])),
            "price": round(bias["price"], 2),
            "trend": q["market_bias"].upper(),
            "rsi": q["rsi"],
            "rsi_state": bias["rsi_state"],
            "macd_state": q["market_state"],
            "atr": q["atr"],
            "ema_20": round(bias["ema_20"], 2),
            "ema_50": round(bias["ema_50"], 2),
            "ema_200": round(bias["ema_200"], 2),
        },
        "swing_count": int((result["quantum"]["data"].tail(len(result["df"]))["break_event"] != 0).sum()),
        "pattern_count": len(result["quantum"]["plans"]),
    }
