# Bug-hunt handoff prompt

Copy everything below the line into a fresh AI session that has read access to
this repository. It is written to be self-contained.

---

You are auditing a live-money algorithmic trading repository for bugs. **Do not
fix anything and do not edit any file.** Your only deliverable is a report.

## What this repo is

`quantum-desk` runs two prop-firm evaluation accounts with the same trading
strategy, split into two completely independent trees:

```
forex/    FTMO 2-Step · MetaTrader 5 · XAUUSD spot · sized in lots     · RUNNING LIVE
future/   TopStep Combine · ProjectX Gateway REST · MGCZ26 micro gold
          · sized in whole contracts · NOT COMMISSIONED
```

Each tree contains its own `bot/` (venue-specific: settings, broker, guardrails,
trader, entry points), `engine/` (venue-neutral: sizing, state, journal, news,
market_hours, instance_lock, dynamic_risk, instrument), `strategy/` (signal
generation + backtest lab), `tools/` (simulators, reports), and `test/`
(`unit/` pytest suite plus `docs/`, `data/`, `outputs/`).

Hard architectural rule: **no import ever crosses between `forex/` and
`future/`**, and `engine` must never import `bot`. Environment prefixes are
separated too — `BOT_*` for forex, `FUT_*` for futures. `engine/` and
`strategy/` are duplicated in both trees on purpose, so a fix in one is not a
fix in the other.

Run tests from inside a tree: `cd forex && python -m pytest test/unit -q`
(278 pass), `cd future && python -m pytest test/unit -q` (48 pass).

## Trading logic you need to understand to judge correctness

- Strategy: `strategy/quantum.py`, traded on M15 and M30 simultaneously.
- Production exit `be_after_tp1_33_33_34`: position splits into three legs
  33/33/34. When TP1 fills, survivors move to a **cost-covered** breakeven
  (actual fill price + commission + slippage + cumulative negative swap, never
  the nominal entry). When TP2 fills, the last leg steps up to lock the **TP1
  level**. The last leg runs to TP3.
- If the position is too small to split three ways (forex: capital under
  $30,000; futures: fewer than 3 contracts) it must run single-leg `fixed_tp3`
  instead — silently degrading to a two-leg approximation is a defect.
- Dynamic risk ladder, identical rule at both venues, different unit:

  | Tier | forex (% of initial capital) | futures (dollars) |
  |---|---|---|
  | drawdown < 0.50% / < $250 | 1.00% | $500 |
  | < 1.00% / < $500 | 0.75% | $375 |
  | < 1.50% / < $750 | 0.50% | $250 |
  | floor | 0.40% | $200 |

  Drawdown is measured from the highest **closed** balance (durable state), with
  current equity on the low side. A floating profit must never ratchet the mark.

- FTMO rules: max loss 10% of initial, **static**; daily loss 5%; targets 10%
  then 5%; 4 minimum trading days.
- TopStep rules: max loss **$2,000 below the highest END-OF-DAY balance,
  trailing**, freezing at the $50,000 starting balance once an end-of-day
  balance reaches $52,000; daily loss $1,000 is a **lockout for the day, not a
  breach**; target $3,000 once; consistency rule — best day over 50% of the
  target **raises the target** to best÷0.5 rather than failing; flat by 15:10
  CT, exchange reopens 17:00 CT; max 5 minis / 50 micros.
- MGC contract: 0.10 tick, $1.00 per tick, so $10.00 per index point. Whole
  contracts only — no rounding up, ever. A risk that does not reach one contract
  must produce **no trade**.

## What was just changed (highest-risk area to audit)

A large refactor plus a brand-new futures venue. In order:

1. `bot/code/` was split into `bot/forex/` + a shared `engine/`, then the whole
   repo was reorganised twice more into the current two-tree layout. Package
   names, `sys.path` bootstrapping, `parents[N]` depths, `.bat` working
   directories, research-data paths (`strategy/config.py`), and template paths
   all moved.
2. `strategy/` was renamed from `xau/`; `tools/` absorbed `scripts/`, `launch/`
   and `templates/`; research data moved under each tree's `test/`.
3. The entire `future/` tree is new code: `bot/settings.py`, `bot/broker.py`
   (ProjectX REST client — **endpoints are unverified, never called against a
   real gateway**), `bot/guardrails.py` (TopStep rules incl. trailing max loss),
   `bot/trader.py` (contract sizing, leg splitting, order submission),
   `bot/terminal.py` (status screen), `bot/live.py`, `tools/topstep_sim.py`.
4. `engine/state.py` gained `eod_balance_high_water`, read by the futures
   guardrails but **currently written by nothing** — the futures run loop does
   not exist yet.

## Bugs already found and fixed — do NOT re-report these

1. `tools/ftmo_portfolio_sim.py` loaded `bot/code/settings.py` via importlib (a
   path string, invisible to a `bot.code` grep).
2. `bot/main.bat` had the wrong `cd` depth and `.venv` path.
3. `test_quantum_entry.py` hardcoded `ROOT/"data"/"market"` instead of
   `config.MARKET_DATA_DIR`.
4. `strategy/webapp.py` pointed at the pre-move `templates/`.
5. `future/test/` had no backtest data, so `topstep_sim.py` could not run.
6. `__pycache__` (71 files) and `.pytest_cache` were committed despite being in
   `.gitignore`.
7. `split_contracts` truncated each leg and dumped the remainder on the runner,
   so 6 contracts came out `(1,1,4)` — a 17/17/66 split pretending to be
   33/33/34. Now largest-remainder: 3→(1,1,1), 6→(2,2,2), 9→(3,3,3).
8. `session_open` only checked Saturday, so Sunday morning read as an open
   session eight hours before the exchange opens.
9. `session_open` blocked everything after 15:10, throwing away the entire
   overnight session the strategy trades.
10. `bot/live.py` passed a naive local timestamp into guardrails, which treats
    naive input as exchange time — 12 hours out on a Bangkok machine.
11. The futures trader had no TP2→TP1 step (only breakeven after TP1).
12. The futures instance had no dynamic risk ladder.

## Your task

Hunt for **additional** bugs, prioritising ones that would lose money or breach
a prop-firm rule. Suggested lines of attack, in rough priority order:

1. **Rule arithmetic.** Re-derive `future/bot/guardrails.py` — `max_loss_floor`,
   `daily_loss_floor`, `internal_daily_floor`, `account_health`, `progress`,
   `consistency`, `can_open`, `can_hold_contracts` — against the TopStep rules
   above. Especially: does the trailing floor freeze correctly, does it ever
   move *down*, is `min_trading_days` meaningful for a Combine that has no such
   requirement, and does a daily-limit hit correctly behave as a lockout rather
   than a permanent halt?
2. **Sizing and order submission.** `future/bot/broker.py::size_contracts`,
   `risk_dollars_for`, `future/bot/trader.py::plan_contracts`,
   `split_contracts`, `open_trade`, `targets_for`, `stop_after_tp1`,
   `stop_after_tp2`, `open_risk_dollars`. Look for: rounding that can ever
   increase risk, the `max_contracts` cap silently changing the exit tier, a
   partial fill leaving unmanaged contracts, legs sent with the wrong target
   order, direction sign errors, and division by zero.
3. **Simulator correctness.** `future/tools/topstep_sim.py` — particularly
   `simulate` vs `simulate_ladder` (do they agree at a fixed tier?), the
   end-of-day floor shift (`floor[:, :-1]` concatenation), the consistency
   target, `apply_daily_stops` truncating only the loss tail, and whether
   pass/fail/unresolved always partition. Same for
   `forex/tools/ftmo_portfolio_sim.py`, which notably does **not** model the
   internal daily stop that the live bot enforces.
4. **Residual path and packaging damage from the refactor.** Stale strings that
   greps for module names would miss (path literals, `.bat` files, docstrings
   used as CLI help, relative markdown links written by report generators),
   `parents[N]` depths, and anything that only breaks at runtime on Windows.
5. **State durability.** `engine/state.py` — atomic save, the new
   `eod_balance_high_water` field, backward compatibility with existing state
   files, and whether a restart can reset a drawdown account to the top risk
   tier.
6. **Cross-tree leakage.** Any import, env var, shared cache file, or absolute
   path that lets one tree read or write the other's data. Also check whether
   the two duplicated `engine/` copies have silently diverged in ways that are
   not deliberate (`decide_dollars` in the futures copy is deliberate).
7. **The live forex tree.** It is running real money and was moved wholesale.
   Confirm nothing in `forex/bot/run.py` or `forex/bot/trader.py` refers to a
   path, module, or file that no longer exists.

Read the code, and where a claim is testable, write a throwaway script to prove
it rather than asserting from inspection. Do not commit anything.

## Deliverable

Return a single prompt that I can paste into another AI session to get the bugs
fixed. It must contain, for each bug found:

- file and line
- what the code does now, and what it should do
- **the concrete failure**: inputs or account state → wrong behaviour → money or
  rule consequence
- how you verified it (the script you ran and its output), or an explicit note
  that it is unverified reasoning
- severity: breaks live money / breaks a prop-firm rule / breaks a tool /
  cosmetic

Order by severity. If you find nothing in a section, say so explicitly rather
than padding. Do not fix anything yourself.
