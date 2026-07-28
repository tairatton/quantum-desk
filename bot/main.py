"""Run the live trading bot immediately; implementation lives in ``bot/code``."""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

if not __package__:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

from bot.code import run
from bot.code.instance_lock import LiveInstanceLock
from bot.code.settings import BOT_DIR
from xau.mt5_source import MT5Error


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
    """Start one live trading process; refuse accidental duplicate instances."""
    instance_lock = LiveInstanceLock(BOT_DIR / ".live.lock")
    if not instance_lock.acquire():
        print("\n" + run.status_line(
            "STATUS",
            "BLOCKED | Another Quantum Desk LIVE process is already running",
            "error"))
        return 2
    try:
        return _run_live()
    finally:
        instance_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
