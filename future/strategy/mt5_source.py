"""MetaTrader 5 data source for XAUUSD.

Design rule: this module never invents data. If the terminal cannot be reached
it raises. Synthetic data is only produced when the caller explicitly asks for
it, and every CSV is written with a sidecar `.meta.json` recording where the
bars came from, so no analysis can silently run on fake prices.
"""
from __future__ import annotations

import dataclasses
import json
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timezone  # noqa: F401  (used by latest_tick)
from pathlib import Path
from typing import Iterator

import numpy as np
import pandas as pd

from . import config

try:
    import MetaTrader5 as mt5

    MT5_IMPORTED = True
except ImportError:  # pragma: no cover - environment dependent
    mt5 = None
    MT5_IMPORTED = False


OHLC_COLUMNS = ["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]


class MT5Error(RuntimeError):
    """Raised when MetaTrader 5 cannot supply the requested data."""


@dataclass
class DataMeta:
    """Provenance record written next to every CSV."""

    source: str  # "mt5" | "yahoo" | "mock"
    symbol: str  # broker name, e.g. "XAUUSDm"
    timeframe: str
    bars: int
    fetched_at: str
    first_bar: str
    last_bar: str
    account: str | None = None
    server: str | None = None
    key: str = "XAUUSD"  # our canonical key, e.g. "XAUUSD" / "BTCUSD"

    @property
    def is_real(self) -> bool:
        return self.source in {"mt5", "yahoo"}


# ---------------------------------------------------------------------------
# Terminal discovery + connection
# ---------------------------------------------------------------------------
def find_terminal() -> Path | None:
    """Locate terminal64.exe, preferring an explicit override."""
    if config.MT5_TERMINAL_PATH:
        p = Path(config.MT5_TERMINAL_PATH)
        if p.exists():
            return p
        raise MT5Error(f"MT5_TERMINAL_PATH points at a missing file: {p}")

    for candidate in config.MT5_TERMINAL_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


@contextmanager
def connection() -> Iterator[object]:
    """Initialise MT5, yield the module, and always shut down afterwards."""
    if not MT5_IMPORTED:
        raise MT5Error(
            "The MetaTrader5 package is not installed.\n"
            "  Fix: pip install MetaTrader5"
        )

    terminal = find_terminal()
    kwargs = {"timeout": config.MT5_INIT_TIMEOUT_MS}
    if terminal is not None:
        kwargs["path"] = str(terminal)

    if not mt5.initialize(**kwargs):
        code, desc = mt5.last_error()
        raise MT5Error(
            f"MT5 initialize() failed: ({code}) {desc}\n"
            f"  terminal tried : {terminal or 'registry auto-discovery'}\n"
            "  Checklist:\n"
            "    1. MetaTrader 5 terminal is open and logged in to an account\n"
            "    2. Tools > Options > Expert Advisors > 'Allow algorithmic trading' is on\n"
            "    3. If the terminal lives somewhere unusual, point us at it:\n"
            "         set MT5_TERMINAL_PATH=C:\\path\\to\\terminal64.exe"
        )

    try:
        info = mt5.terminal_info()
        if info is not None and not info.connected:
            raise MT5Error("MT5 terminal is running but not connected to the broker.")
        yield mt5
    finally:
        mt5.shutdown()


def latest_tick(symbol: str = config.DEFAULT_SYMBOL) -> dict | None:
    """Current bid/ask plus the broker's own timestamp for that quote.

    The tick time is what tells a live market from a closed one: over a
    weekend it simply stops advancing, and the UI can say so instead of
    looking frozen.
    """
    with connection() as mt:
        resolved = resolve_symbol(mt, symbol.upper())
        t = mt.symbol_info_tick(resolved)
        if t is None:
            return None
        return {
            "symbol": resolved,
            "bid": float(t.bid),
            "ask": float(t.ask),
            "time": datetime.fromtimestamp(t.time, tz=timezone.utc)
            .replace(tzinfo=None).isoformat(sep=" ", timespec="seconds"),
        }


def resolve_symbol(mt, key: str) -> str:
    """Map a symbol key ("XAUUSD") to the broker's actual name ("XAUUSDm")."""
    cfg = config.symbol_cfg(key)
    wanted = cfg["mt5"]

    for name in [wanted, *cfg["fallbacks"]]:
        if mt.symbol_info(name) is not None:
            if not mt.symbol_select(name, True):
                continue
            if name != wanted:
                print(f"[MT5] '{wanted}' unavailable - using '{name}' instead.")
            return name

    stem = key.upper()[:3]
    available = [s.name for s in (mt.symbols_get(f"*{stem}*") or [])]
    raise MT5Error(
        f"No tradable symbol found for '{key}'.\n"
        f"  Matching symbols in this account: {available or 'none'}\n"
        f"  Fix the mapping in strategy/config.py -> SYMBOLS['{key.upper()}']['mt5']"
    )


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def fetch(
    symbol: str = config.DEFAULT_SYMBOL,
    timeframe: str = config.DEFAULT_TIMEFRAME,
    bars: int = config.DEFAULT_BARS,
) -> tuple[pd.DataFrame, DataMeta]:
    """Pull `bars` candles from MT5. Raises MT5Error rather than faking data."""
    tf_key = timeframe.upper()
    if tf_key not in config.TIMEFRAMES:
        raise MT5Error(
            f"Unknown timeframe '{timeframe}'. Known: {', '.join(config.TIMEFRAMES)}"
        )
    key = symbol.upper()

    with connection() as mt:
        resolved = resolve_symbol(mt, key)
        account = mt.account_info()

        print(f"[MT5] Fetching {bars} x {tf_key} bars for {resolved} ...")
        rates = mt.copy_rates_from_pos(resolved, config.TIMEFRAMES[tf_key], 0, bars)

        if rates is None or len(rates) == 0:
            code, desc = mt.last_error()
            raise MT5Error(
                f"MT5 returned no bars for {resolved} {tf_key}: ({code}) {desc}\n"
                "  The symbol may need its chart opened once in the terminal to "
                "download history."
            )

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df = _normalise(df)

        meta = DataMeta(
            source="mt5",
            symbol=resolved,
            timeframe=tf_key,
            bars=len(df),
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            first_bar=str(df["time"].iloc[0]),
            last_bar=str(df["time"].iloc[-1]),
            account=str(account.login) if account else None,
            server=account.server if account else None,
            key=key,
        )

    print(f"[MT5] OK - {len(df)} bars, {meta.first_bar} -> {meta.last_bar}")
    return df, meta


def generate_mock(
    symbol: str = config.DEFAULT_SYMBOL,
    timeframe: str = config.DEFAULT_TIMEFRAME,
    bars: int = config.DEFAULT_BARS,
    seed: int = 42,
) -> tuple[pd.DataFrame, DataMeta]:
    """Synthetic random-walk candles. For offline testing only - never a fallback."""
    print(f"[MOCK] Generating {bars} synthetic {timeframe} bars - NOT REAL PRICES.")
    freq = {
        "M1": "1min", "M5": "5min", "M15": "15min", "M30": "30min",
        "H1": "1h", "H4": "4h", "D1": "1D", "W1": "1W",
    }[timeframe.upper()]

    end = pd.Timestamp.now().floor(freq)
    times = pd.date_range(end=end, periods=bars, freq=freq)

    rng = np.random.default_rng(seed)
    close = 2400.0 * np.exp(np.cumsum(rng.normal(0.0001, 0.003, bars)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    wick = np.abs(rng.normal(0, 0.002, bars)) * close

    df = pd.DataFrame({
        "time": times,
        "open": open_,
        "high": np.maximum(open_, close) + wick,
        "low": np.minimum(open_, close) - wick,
        "close": close,
        "tick_volume": rng.integers(500, 5000, bars),
        "spread": rng.integers(10, 30, bars),
        "real_volume": 0,
    })
    df = _normalise(df)

    meta = DataMeta(
        source="mock",
        symbol=symbol,
        timeframe=timeframe.upper(),
        bars=len(df),
        fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        first_bar=str(df["time"].iloc[0]),
        last_bar=str(df["time"].iloc[-1]),
        key=symbol.upper(),
    )
    return df, meta


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    for col in OHLC_COLUMNS:
        if col not in df.columns:
            df[col] = 0
    df = df[OHLC_COLUMNS].copy()
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df.sort_values("time").drop_duplicates("time").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def csv_path(symbol: str, timeframe: str) -> Path:
    return config.MARKET_DATA_DIR / symbol.upper() / f"{timeframe.upper()}.csv"


def meta_path(symbol: str, timeframe: str) -> Path:
    return config.MARKET_DATA_DIR / symbol.upper() / f"{timeframe.upper()}.meta.json"


def save(df: pd.DataFrame, meta: DataMeta) -> Path:
    """Persist bars, merging with whatever history is already on disk.

    The dashboard refreshes with a small window (1500 bars). Overwriting would
    let each poll truncate a 6000-bar archive down to that window, quietly
    degrading every later backtest. Merging keeps the deepest history we have
    ever seen and lets it accumulate instead.
    """
    config.ensure_dirs()
    path = csv_path(meta.key, meta.timeframe)
    path.parent.mkdir(parents=True, exist_ok=True)

    combined = df
    if path.exists() and meta.source == "mt5":
        try:
            old = pd.read_csv(path)
            old["time"] = pd.to_datetime(old["time"])
            prev_meta = meta_path(meta.key, meta.timeframe)
            # Never merge real bars into mock ones (or the reverse).
            same_source = True
            if prev_meta.exists():
                same_source = json.loads(
                    prev_meta.read_text(encoding="utf-8")
                ).get("source") == "mt5"
            if same_source:
                combined = (
                    pd.concat([old, df], ignore_index=True)
                    .drop_duplicates(subset="time", keep="last")
                    .sort_values("time")
                    .reset_index(drop=True)
                )
        except (ValueError, OSError, KeyError):
            combined = df  # unreadable archive - start fresh rather than fail

    combined.to_csv(path, index=False)

    stored = dataclasses.replace(
        meta,
        bars=len(combined),
        first_bar=str(combined["time"].iloc[0]),
        last_bar=str(combined["time"].iloc[-1]),
    )
    meta_path(meta.key, meta.timeframe).write_text(
        json.dumps(asdict(stored), indent=2), encoding="utf-8"
    )
    grew = len(combined) - len(df)
    extra = f"  (+{grew} kept from archive)" if grew > 0 else ""
    print(f"[SAVED] {path}  ({meta.source}, {len(combined)} bars){extra}")
    return path


def load(symbol: str, timeframe: str) -> tuple[pd.DataFrame, DataMeta | None]:
    """Read cached bars. Loudly flags mock data instead of hiding it."""
    path = csv_path(symbol, timeframe)
    if not path.exists():
        raise FileNotFoundError(
            f"No cached data at {path}\n"
            f"  Fetch it first:  python main.py fetch {timeframe} --symbol {symbol}"
        )

    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["time"])

    meta = None
    mp = meta_path(symbol, timeframe)
    if mp.exists():
        meta = DataMeta(**json.loads(mp.read_text(encoding="utf-8")))
        if not meta.is_real:
            print("=" * 68)
            print(" WARNING: this dataset is MOCK data - analysis below is meaningless")
            print(f"          regenerate with:  python main.py fetch {timeframe} "
                  f"--symbol {symbol}")
            print("=" * 68)
    else:
        print(f"[WARN] {path.name} has no provenance file - origin unknown.")

    return df, meta


def get_data(
    symbol: str = config.DEFAULT_SYMBOL,
    timeframe: str = config.DEFAULT_TIMEFRAME,
    bars: int = config.DEFAULT_BARS,
    refresh: bool = False,
) -> tuple[pd.DataFrame, DataMeta | None]:
    """Load cached bars, fetching from MT5 when missing or when refresh is set."""
    if not refresh and csv_path(symbol, timeframe).exists():
        return load(symbol, timeframe)

    df, meta = fetch(symbol=symbol, timeframe=timeframe, bars=bars)
    save(df, meta)
    return df, meta
