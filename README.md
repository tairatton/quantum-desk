# Quantum Desk

Two prop-firm trading systems, kept completely apart.

```
bot/forex/     FTMO · MetaTrader 5 · XAUUSD spot, sized in lots
bot/future/    TopStep · ProjectX Gateway · MGCZ26 micro gold, sized in contracts
```

Each tree is self-contained and runs from its own directory. The tree root
holds only what a person needs to start the bot; everything else is under
`core/`:

```
bot/<tree>/
  main.py     double-click-friendly menu (delegates into core/entrypoints)
  main.bat    double-click launcher — forex starts the live loop directly,
              future opens the terminal menu
  core/
    entrypoints/ the real main/live/research modules main.py and main.bat call
    bot/        live execution: settings, broker, guardrails, run, trader, live
    engine/     sizing, state, journal, news, sessions, instance lock
    strategy/   signal generation and the backtest lab
    tools/      simulators, reports, launchers
    test/       unit/  ·  docs  ·  data  ·  outputs
```

There is no shared code at the root and no import crosses between the trees.
The two firms do not share a rulebook — FTMO's max loss is static and measured
from the initial balance, TopStep's trails the highest end-of-day balance — so a
change made for futures cannot reach the live FTMO account, and neither can a
stray environment variable: the forex tree reads `BOT_*`, the futures tree reads
`FUT_*`.

The cost of that separation is duplication: `engine/` and `strategy/` exist in
both trees, so a fix in one is not a fix in the other.

Full system documentation — what it does, measured results, and every
known risk — is in [DOCUMENT.md](DOCUMENT.md).

## Installing

One file, both trees:

```bash
python -m pip install -r requirements.txt
```

The live-trading packages are pinned to what the running account uses; the
reporting ones are loose. `MetaTrader5` is Windows-only and needed by the forex
tree alone — the futures tree reaches ProjectX over HTTPS with the standard
library.

## Running

Double-click `bot/forex/main.bat` or `bot/future/main.bat`, or from a shell:

```bash
cd bot/forex
python main.py                      # menu: status, dry run, live, stop, journal
cd core
python -m bot.run --status          # account, guards, live R stats
python -m bot.run --once            # one pass, dry run
python -m bot.run --live            # sends real orders
python -m pytest test/unit -q
python tools/ftmo_portfolio_sim.py  # pass rate and drawdown against FTMO rules
```

```bash
cd bot/future
python main.py                      # menu: status, connection test, kill switch
python main.py --status --offline   # one status screen, no network
cd core
python -m entrypoints.live --check  # read-only ProjectX connection test
python -m entrypoints.live          # dry run
python -m pytest test/unit -q
python tools/topstep_sim.py         # pass rate against TopStep's trailing rules
```

The futures tree is **not commissioned**: `--live` refuses until the ProjectX
endpoints have been exercised against a demo key, TopStep's limits have been
confirmed against the current rulebook, and the strategy has been re-measured on
MGC data rather than borrowed from XAUUSD spot. See
`bot/future/core/test/docs/TOPSTEP_RULES_AND_SIM.md`.

## Credentials

Never in the repo. MT5: `BOT_MT5_LOGIN`, `BOT_MT5_PASSWORD`, `BOT_MT5_SERVER`.
ProjectX: `FUT_PROJECTX_USERNAME`, `FUT_PROJECTX_API_KEY`.
