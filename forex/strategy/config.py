"""Central configuration for the HTF Quantum Adaptive system.

Every path, symbol and default lives here so the rest of the package never
hard-codes a location.
"""
from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Research lives under this tree's own `test/`. There is no venue switch and no
# way to point it at the other tree: `forex/` and `future/` are separate trees
# with separate copies of this file, which is the whole reason a backtest here
# cannot silently read the other venue's data.
VENUE = PROJECT_ROOT.name

RESEARCH_ROOT = PROJECT_ROOT / "test"
DATA_DIR = RESEARCH_ROOT / "data"
MARKET_DATA_DIR = DATA_DIR / "market"
AI_DECISION_DIR = DATA_DIR / "ai_decisions"

OUTPUT_DIR = RESEARCH_ROOT / "outputs"
BACKTEST_DIR = OUTPUT_DIR / "backtests"
TECHNIQUE_REPORT_DIR = BACKTEST_DIR / "technique_lab"
AI_REPORT_DIR = BACKTEST_DIR / "ai"
QUANTUM_REPORT_DIR = BACKTEST_DIR / "quantum"
CHART_DIR = OUTPUT_DIR / "charts" / "analysis"
FTMO_CHART_DIR = OUTPUT_DIR / "charts" / "ftmo"
DOCS_DIR = RESEARCH_ROOT / "docs"

# Compatibility alias for modules that refer to the MT5 cache root.
RAW_DIR = MARKET_DATA_DIR


def ensure_dirs() -> None:
    for d in (
        MARKET_DATA_DIR, AI_DECISION_DIR, TECHNIQUE_REPORT_DIR,
        AI_REPORT_DIR, QUANTUM_REPORT_DIR, CHART_DIR, FTMO_CHART_DIR, DOCS_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Symbols
# --------------------------------------------------------------------------
# `weekend_gap` matters: metals trade 24/5 so Sat/Sun must be hidden on the
# x-axis and skipped when projecting forward. Crypto trades 24/7 - applying the
# same break there would delete real candles.
SYMBOLS: dict[str, dict] = {
    "XAUUSD": {
        "label": "Gold",
        "mt5": "XAUUSDm",
        "fallbacks": ["XAUUSDm", "XAUUSD", "XAUUSD.m", "XAUUSD_", "GOLD", "GOLDm"],
        "weekend_gap": True,
        "decimals": 3,
    },
    "BTCUSD": {
        "label": "Bitcoin",
        "mt5": "BTCUSDm",
        "fallbacks": ["BTCUSDm", "BTCUSD", "BTCUSD.m", "BTCUSDT", "BITCOIN"],
        "weekend_gap": False,
        "decimals": 2,
    },
    "EURUSD": {
        "label": "Euro / U.S. Dollar",
        "mt5": "EURUSDm",
        "fallbacks": ["EURUSDm", "EURUSD", "EURUSD.m", "EURUSD_"],
        "weekend_gap": True,
        "decimals": 5,
    },
    "GBPUSD": {
        "label": "British Pound / U.S. Dollar",
        "mt5": "GBPUSDm",
        "fallbacks": ["GBPUSDm", "GBPUSD", "GBPUSD.m", "GBPUSD_"],
        "weekend_gap": True,
        "decimals": 5,
    },
    "USDJPY": {
        "label": "U.S. Dollar / Japanese Yen",
        "mt5": "USDJPYm",
        "fallbacks": ["USDJPYm", "USDJPY", "USDJPY.m", "USDJPY_"],
        "weekend_gap": True,
        "decimals": 3,
    },
}

# Round-trip trading cost the price feed does not contain.
#
# How many spreads a round trip pays is worth getting right, because doubling it
# is enough to erase every FX result in the study. MT5 bars are BID prices —
# verified: a live M1 close matched the bid exactly, not the ask. So a long buys
# at ask (one spread) and its exits are compared against bid, as recorded, at no
# further cost; a short enters at bid for free and pays one spread on the way out.
# Either way a round trip costs the spread ONCE, which is what the lab already
# charged. `spread_sides` stays configurable only because a broker quoting bars at
# mid would need 2.
#
# What was genuinely missing is commission and slippage, neither of which appears
# in the feed:
#
#   spread_sides       times the bar's spread is charged (1 for bid-quoted bars)
#   commission_per_lot account currency per 1.0 lot, round trip
#   value_per_point    account currency per 1.0 price point per 1.0 lot, needed to
#                      turn a cash commission into R
#   slippage_points    expected points lost over the whole round trip; used by
#                      research/backtest expectancy.
#   breakeven_slippage_points
#                      conservative stop-fill reserve used only when live SL is
#                      moved to BE/a profit step. This is a tail allowance, not
#                      an average cost. XAU is calibrated above the observed
#                      $0.43 adverse stop fill on 2026-08-05.
#
# These are estimates of the right order of magnitude, not measurements. Replace
# them with what an FTMO demo actually charges — that is what phase 2 of the plan
# in docs/FTMO_SYMBOL_AND_TIMELINE.md is for.
COSTS: dict[str, dict] = {
    "XAUUSD": {"spread_sides": 1, "commission_per_lot": 7.0,
               "value_per_point": 100.0, "slippage_points": 50,
               "breakeven_slippage_points": 500},                    # $0.50
    "BTCUSD": {"spread_sides": 1, "commission_per_lot": 0.0,
               "value_per_point": 1.0, "slippage_points": 500},       # $5
    "EURUSD": {"spread_sides": 1, "commission_per_lot": 7.0,
               "value_per_point": 100_000.0, "slippage_points": 5},   # 0.5 pip
    "GBPUSD": {"spread_sides": 1, "commission_per_lot": 7.0,
               "value_per_point": 100_000.0, "slippage_points": 5},
    # 1 lot = 100,000 USD, so a 1.00 JPY move is 100000/rate ~ $667 at 150.
    "USDJPY": {"spread_sides": 1, "commission_per_lot": 7.0,
               "value_per_point": 667.0, "slippage_points": 5},
}

DEFAULT_COSTS = {"spread_sides": 1, "commission_per_lot": 0.0,
                 "value_per_point": 1.0, "slippage_points": 0}


def cost_cfg(key: str) -> dict:
    return COSTS.get(key.upper(), DEFAULT_COSTS)


DEFAULT_SYMBOL = os.getenv("XAU_DEFAULT_SYMBOL", "XAUUSD")

# Legacy single-symbol override, still honoured.
SYMBOL = os.getenv("XAU_SYMBOL", SYMBOLS[DEFAULT_SYMBOL]["mt5"])


def symbol_cfg(key: str) -> dict:
    """Look up a symbol's config by key (case-insensitive)."""
    k = key.upper()
    if k not in SYMBOLS:
        raise KeyError(f"Unknown symbol '{key}'. Known: {', '.join(SYMBOLS)}")
    return SYMBOLS[k]

# --------------------------------------------------------------------------
# Timeframes  (values are the MetaTrader5 TIMEFRAME_* constants)
# --------------------------------------------------------------------------
TIMEFRAMES: dict[str, int] = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 16385,
    "H4": 16388,
    "D1": 16408,
    "W1": 32769,
}

DEFAULT_TIMEFRAME = "H1"
DEFAULT_BARS = 1500

# Timeframes offered by the web UI, in display order.
WEB_TIMEFRAMES = ["M5", "M15", "M30", "H1", "H4"]

# Human labels for the UI buttons.
TIMEFRAME_LABELS = {
    "M5": "5m", "M15": "15m", "M30": "30m",
    "H1": "1H", "H4": "4H", "D1": "1D", "W1": "1W",
}

# Seconds per bar - used to size the cache TTL.
TIMEFRAME_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400, "W1": 604800,
}

# --------------------------------------------------------------------------
# MetaTrader 5 terminal discovery
# --------------------------------------------------------------------------
# mt5.initialize() finds the terminal through the Windows registry. That lookup
# fails for portable / non-standard installs, so we pass an explicit path.
# Override with:  set MT5_TERMINAL_PATH=D:\path\to\terminal64.exe
MT5_TERMINAL_PATH = os.getenv("MT5_TERMINAL_PATH")

MT5_TERMINAL_CANDIDATES = [
    Path(os.path.expandvars(r"%APPDATA%\MetaTrader 5\terminal64.exe")),
    Path(r"C:\Program Files\MetaTrader 5\terminal64.exe"),
    Path(r"C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe"),
    Path(r"C:\Program Files\Exness MetaTrader 5\terminal64.exe"),
    Path(r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe"),
]

MT5_INIT_TIMEOUT_MS = 30_000

# Bars shown on the chart.
DEFAULT_PLOT_BARS = 300
