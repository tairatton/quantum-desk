"""Start the FOREX instance live: FTMO rules, MT5 session, XAUUSD.

The futures instance has its own entry point in ``bot/live.py``. Neither
starts the other, and each holds its own single-instance lock, so running both
at once is a supported thing to do rather than an accident waiting to happen.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

# Each tree is its own import root: `bot`, `engine`, `strategy` and `tools` are
# top-level packages *within* it. Running `python -m forex.bot.live` from the
# repository root therefore cannot work -- Python would import
# `forex.bot`, whose own `from bot import ...` then resolves to a different,
# unrelated package. That failure used to surface as a bare ImportError deep in
# the module; it is caught here and explained instead.
if __package__ and __package__ != "bot":
    raise SystemExit(
        "Run this from inside the tree, not from the repository root:\n"
        "    cd forex && python -m bot.live\n"
        "Each tree is a separate import root, so `python -m forex.bot.live` "
        "would bind `bot` to the wrong package."
    )
if not __package__:
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

from bot import run
from strategy.mt5_source import MT5Error


def _run_live() -> int:
    print("\n" + run.status_line(
        "STATUS", "STARTING | LIVE trading | Press Ctrl+C to stop", "live"))
    while True:
        try:
            print(run.status_line(
                "STATUS", f"{datetime.now():%Y-%m-%d %H:%M:%S} | Connecting to MT5...", "info"))
            run.execute(live=True)
            return 0
        except MT5Error as error:
            print(run.status_line("STATUS", f"WAITING | MT5 is not ready: {error}", "warn"))
            print(run.status_line(
                "STATUS",
                "Open MT5, log in, and enable Algo Trading | Retrying in 10 seconds",
                "warn"))
            time.sleep(10)
        except KeyboardInterrupt:
            print("\n" + run.status_line(
                "STATUS", "STOPPED | Open positions retain their broker-side SL/TP", "warn"))
            return 0


def main() -> int:
    """Start live trading; the shared execution layer enforces one instance."""
    return _run_live()


if __name__ == "__main__":
    raise SystemExit(main())
