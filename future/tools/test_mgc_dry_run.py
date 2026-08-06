"""Run the futures strategy and whole-contract sizing without a broker.

This is a smoke test for the bot path, not a live gateway test: it reads the
free Yahoo ``MGC=F`` files produced by ``download_mgc_yahoo.py``, evaluates the
same signal state machine, applies the dynamic risk tier, and calls
``plan_contracts``.  No credentials and no order endpoint are used.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import settings as settings_module  # noqa: E402
from bot.trader import OrderRejected, plan_contracts  # noqa: E402
from engine.dynamic_risk import decide_dollars  # noqa: E402
from engine.state import BotState  # noqa: E402
from strategy.quantum import analyse  # noqa: E402


def run(equity: float | None = None) -> int:
    settings = settings_module.load()
    initial = float(settings.initial_balance or settings.account_size)
    state = BotState(initial_balance=initial, balance_high_water=initial)
    current_equity = initial if equity is None else float(equity)
    decision = decide_dollars(settings, state, current_equity)
    print(f"[DRY-RUN] MGC Yahoo bars | dynamic={settings.dynamic_risk_enabled} "
          f"drawdown=${decision.drawdown_percent:.0f} "
          f"requested_risk=${decision.risk_percent:.0f}")

    for timeframe in ("M15", "M30"):
        path = ROOT / "test" / "data" / "market" / "MGC" / f"{timeframe}.csv"
        if not path.exists():
            print(f"[MISSING] {path} -- run download_mgc_yahoo.py first")
            return 2
        frame = pd.read_csv(path, parse_dates=["time"])
        result = analyse(frame, timeframe)
        filled = [plan for plan in result["plans"]
                  if plan.get("entry_fill_index") is not None]
        sized = 0
        skipped = 0
        contracts: list[int] = []
        reason = ""
        for plan in filled:
            try:
                sized_plan = plan_contracts(
                    settings, plan["entry"], plan["stop"], decision.risk_percent)
                sized += 1
                contracts.append(sized_plan.contracts)
            except OrderRejected as error:
                skipped += 1
                reason = reason or str(error)
        print(f"{timeframe}: bars={len(frame)} plans={len(result['plans'])} "
              f"filled={len(filled)} sized={sized} skipped={skipped} "
              f"contracts={sorted(set(contracts))}")
        if reason:
            print(f"  skip_example: {reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--equity", type=float,
                        help="preview another equity level; default is account high-water")
    args = parser.parse_args(argv)
    return run(args.equity)


if __name__ == "__main__":
    raise SystemExit(main())
