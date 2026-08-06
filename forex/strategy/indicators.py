"""Technical indicators used by HTF Quantum Adaptive.

All functions are pure: they take a DataFrame and return a new one.
"""
from __future__ import annotations

import pandas as pd

__all__ = ["atr", "ema", "rsi", "macd", "bollinger", "add_all", "trend_bias"]


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder-smoothed Average True Range."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift()
    true_range = pd.concat([
        high - low, (high - prev_close).abs(), (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False).mean()


def ema(df: pd.DataFrame, periods: tuple[int, ...] = (20, 50, 200)) -> pd.DataFrame:
    df = df.copy()
    for p in periods:
        df[f"ema_{p}"] = df["close"].ewm(span=p, adjust=False).mean()
    return df


def rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Wilder's RSI (the original uses smoothed averages, not a simple mean)."""
    df = df.copy()
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    # Where avg_loss is 0 the RSI is 100 by definition; guard the division
    # rather than dividing by zero and patching NaNs afterwards.
    rs = avg_gain / avg_loss.where(avg_loss > 0)
    out = (100 - 100 / (1 + rs)).fillna(100.0).astype(float)

    # The first `period` bars have no meaningful average yet. Leaving them at
    # the filled 100.0 draws a fake spike at the left edge of every chart.
    out.iloc[:period] = float("nan")

    df[f"rsi_{period}"] = out
    return df


def macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    df = df.copy()
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    df["macd_line"] = ema_fast - ema_slow
    df["macd_signal"] = df["macd_line"].ewm(span=signal, adjust=False).mean()
    df["macd_hist"] = df["macd_line"] - df["macd_signal"]
    return df


def bollinger(df: pd.DataFrame, period: int = 20, mult: float = 2.0) -> pd.DataFrame:
    df = df.copy()
    mid = df["close"].rolling(period).mean()
    std = df["close"].rolling(period).std()
    df["bb_middle"], df["bb_upper"], df["bb_lower"] = mid, mid + std * mult, mid - std * mult
    return df


def add_all(df: pd.DataFrame) -> pd.DataFrame:
    out = ema(df)
    out = rsi(out)
    out = macd(out)
    out = bollinger(out)
    out["atr_14"] = atr(df, 14)
    return out


def trend_bias(df: pd.DataFrame) -> dict:
    """Compact trend read used by the report header."""
    d = add_all(df)
    last, prev = d.iloc[-1], d.iloc[-2]
    price = last["close"]

    if price > last["ema_20"] > last["ema_50"] > last["ema_200"]:
        trend = "STRONG BULLISH"
    elif price > last["ema_50"]:
        trend = "BULLISH"
    elif price < last["ema_20"] < last["ema_50"] < last["ema_200"]:
        trend = "STRONG BEARISH"
    elif price < last["ema_50"]:
        trend = "BEARISH"
    else:
        trend = "SIDEWAYS"

    r = last["rsi_14"]
    rsi_state = "OVERBOUGHT" if r >= 70 else "OVERSOLD" if r <= 30 else "NEUTRAL"

    if last["macd_line"] > last["macd_signal"] and prev["macd_line"] <= prev["macd_signal"]:
        macd_state = "BULLISH CROSSOVER"
    elif last["macd_line"] < last["macd_signal"] and prev["macd_line"] >= prev["macd_signal"]:
        macd_state = "BEARISH CROSSOVER"
    else:
        macd_state = "BULLISH ZONE" if last["macd_line"] > last["macd_signal"] else "BEARISH ZONE"

    return {
        "time": last["time"],
        "price": float(price),
        "trend": trend,
        "rsi": float(r),
        "rsi_state": rsi_state,
        "macd_state": macd_state,
        "atr": float(last["atr_14"]),
        "ema_20": float(last["ema_20"]),
        "ema_50": float(last["ema_50"]),
        "ema_200": float(last["ema_200"]),
        "data": d,
    }
