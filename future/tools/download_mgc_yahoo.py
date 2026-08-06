"""Download free Micro Gold smoke-test bars from Yahoo Finance.

This is deliberately a read-only research source for bot dry-runs.  ``MGC=F``
is a delayed, rolled Yahoo series; it is not the Topstep/ProjectX execution
feed and must not be used to commission the live gateway.

Examples (run from ``future``)::

    python tools/download_mgc_yahoo.py --period 60d
    python tools/download_mgc_yahoo.py --period 60d --interval 15m

The files are written under ``test/data/market/MGC`` in the same OHLC shape as
the existing strategy data, with a provenance sidecar next to each CSV.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy import config  # noqa: E402

SYMBOL = "MGC=F"
OUTPUT_KEY = "MGC"
INTERVALS = {"15m": "M15", "30m": "M30"}


def _download(interval: str, period: str) -> pd.DataFrame:
    if interval not in INTERVALS:
        raise ValueError(f"unsupported interval {interval!r}; use 15m or 30m")
    frame = yf.download(
        SYMBOL,
        period=period,
        interval=interval,
        auto_adjust=False,
        actions=False,
        progress=False,
        threads=False,
    )
    if frame.empty:
        raise RuntimeError(f"Yahoo returned no bars for {SYMBOL} {interval}")
    # yfinance returns a two-level column index even for one ticker in some
    # versions.  The first level is the OHLC field we need.
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    required = ("Open", "High", "Low", "Close", "Volume")
    missing = [name for name in required if name not in frame.columns]
    if missing:
        raise RuntimeError(f"Yahoo response is missing columns: {missing}")

    index = pd.to_datetime(frame.index, utc=True).tz_convert(None)
    out = pd.DataFrame({
        "time": index,
        "open": pd.to_numeric(frame["Open"], errors="coerce"),
        "high": pd.to_numeric(frame["High"], errors="coerce"),
        "low": pd.to_numeric(frame["Low"], errors="coerce"),
        "close": pd.to_numeric(frame["Close"], errors="coerce"),
        "tick_volume": pd.to_numeric(frame["Volume"], errors="coerce").fillna(0),
        # Yahoo does not publish the broker spread.  Zero is explicit here;
        # this dataset is for signal/sizing smoke tests, not cost validation.
        "spread": 0.0,
        "real_volume": pd.to_numeric(frame["Volume"], errors="coerce").fillna(0),
    })
    out = (out.dropna(subset=["open", "high", "low", "close"])
              .sort_values("time")
              .drop_duplicates("time")
              .reset_index(drop=True))
    if len(out) < 80:
        raise RuntimeError(f"Yahoo returned only {len(out)} usable bars")
    return out


def save(interval: str, period: str) -> Path:
    timeframe = INTERVALS[interval]
    frame = _download(interval, period)
    folder = config.MARKET_DATA_DIR / OUTPUT_KEY
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{timeframe}.csv"
    frame.to_csv(path, index=False)
    meta = {
        "source": "yahoo",
        "symbol": SYMBOL,
        "timeframe": timeframe,
        "bars": len(frame),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "first_bar": str(frame["time"].iloc[0]),
        "last_bar": str(frame["time"].iloc[-1]),
        "key": OUTPUT_KEY,
    }
    (folder / f"{timeframe}.meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    print(f"[SAVED] {path} | {len(frame)} bars | {meta['first_bar']} -> {meta['last_bar']}")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", default="60d",
                        help="Yahoo period, e.g. 60d (intraday history is limited)")
    parser.add_argument("--interval", choices=tuple(INTERVALS) ,
                        help="download one interval; default downloads M15 and M30")
    args = parser.parse_args(argv)
    intervals = (args.interval,) if args.interval else tuple(INTERVALS)
    for interval in intervals:
        save(interval, args.period)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
