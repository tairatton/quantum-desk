"""Entry point for the FUTURES instance: TopStep on the ProjectX Gateway.

    python -m bot.live --check     read-only connection test
    python -m bot.live             dry run, sends nothing
    python -m bot.live --live      refuses until commissioned

The forex instance starts from `bot/live.py` and the two never start each
other. Running both at once is supported: separate settings, state, journal and
lock file.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if not __package__:
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

from bot import guardrails, settings as settings_module
from bot.broker import Broker, ProjectXError

# Flipped to True only after `--check` has run clean against the gateway, the
# loss limits in settings.local.json have been confirmed against TopStep's
# rulebook, and the strategy has been re-measured on futures data. Until then
# `--live` refuses: an untested order path on a funded account is not a risk
# worth taking to save one commit.
COMMISSIONED = False


def check(settings) -> int:
    """Read-only: authenticate, name the account and contract, count positions."""
    broker = Broker(settings, dry_run=True)
    try:
        print(json.dumps(broker.verify_connection(), indent=2))
    except ProjectXError as error:
        print(f"[FUTURES] connection check failed: {error}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="read-only connection test, then exit")
    parser.add_argument("--live", action="store_true",
                        help="send real orders (refused until commissioned)")
    args = parser.parse_args(argv)

    settings = settings_module.load()
    if args.check:
        return check(settings)

    if args.live and not COMMISSIONED:
        print("[FUTURES] --live is refused: this instance is not commissioned.\n"
              "          1. python -m bot.live --check\n"
              "          2. confirm the loss limits against TopStep's rulebook\n"
              "          3. re-measure the strategy on MGC data in test/data/\n"
              "          then set COMMISSIONED = True in bot/live.py")
        return 2

    # UTC, not local: `session_open` converts to the exchange clock, and a naive
    # local timestamp would be taken AS exchange time -- on a Bangkok machine
    # that is twelve hours out, so the session gate would have been answering
    # about the wrong half of the day.
    verdict = guardrails.session_open(settings, datetime.now(timezone.utc))
    print(f"[FUTURES] dry run | {settings.contract_symbol} | "
          f"risk ${settings.risk_dollars:,.0f} | session: {verdict.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
