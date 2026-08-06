# TopStep Combine: the rules, and what they do to this system

Checked August 2026. Every figure here is a rule that ends or funds an account,
so re-confirm against TopStep's own rulebook before trusting a simulation built
on it — these change, and a stale number makes the simulator confidently wrong.

## The rules, and how they differ from FTMO

| | FTMO 2-Step | TopStep 50K Combine |
|---|---|---|
| Profit target | 10% then 5% | **$3,000**, once |
| Max loss | 10% of initial, **static** | **$2,000 below the highest end-of-day balance, trailing** |
| Trailing freeze | n/a | once an end-of-day balance reaches **$52,000**, the floor freezes at $50,000 |
| Daily loss | 5% → **breach** | **$1,000 → locked out for the day**, account survives |
| Daily reset | midnight Prague | **5:00 PM CT** |
| Minimum days | 4 trading days | none for the Combine (the $150 winning-day rule belongs to the funded stage) |
| Consistency | none | best day ≤ 50% of target, else the **target rises** to best ÷ 0.5 — it does not fail |
| Flat by | none | **3:10 PM CT** every weekday, resume 5:00 PM CT |
| Position cap | risk-based | 5 minis / **50 micros** simultaneously |

The trailing floor is the whole story. Under FTMO a good week buys permanent
room. Under TopStep a good week **moves the floor up behind you**, so giving
back what was just made can end an account that is still in profit. That is why
`bot/guardrails.py` reads `trailing_max_loss` rather than reusing the forex
logic, and why `max_loss_floor` freezes at the starting balance.

Two rules are gentler than FTMO and are worth naming, because they change what
"failure" means: hitting the daily loss limit is a lockout, not a breach, and
breaking consistency raises the bar instead of ending the attempt.

## Simulation

`python tools/topstep_sim.py` — 20,000 paths, 400 simulated trading days, the
production dynamic ladder plus remaining-room/reserve guard, whole-contract MGC
sizing, the production fixed-TP3/split exit switch, and the bot's own $400
internal daily stop applied on top of the firm's $1,000. By default it samples
the two strategy streams from locally cached Yahoo `MGC=F` bars. `--risk` and
`--flat` are fixed-risk reference experiments; they are not the live bot's risk
path. The simulator is MGC-only; it never reads the Forex/XAUUSD reports.

Latest MGC Yahoo 60-day smoke result (run date 2026-08-06):

| Regime | PASS | FAIL | open | days med/p90 | DD med | DD p95 |
|---|---:|---:|---:|---:|---:|---:|
| MGC Yahoo `yahoo_60d` | **100.0%** | 0.0% | 0.0% | **13 / 28** | $454 | $960 |

This is a short, rolled Yahoo sample, so the percentages are not a reliable
long-run pass probability. It is useful for checking that the futures signal,
sizing and risk-guard path behave on MGC-shaped bars. Forex/XAUUSD reports are
kept in the separate `forex/` tree and are not mixed into this result.

For the historical fixed-risk table, run `python tools/topstep_sim.py --flat`.

### Free bot smoke test (not a Topstep feed)

When API Access is not enabled, the bot path can still be exercised with
Yahoo's delayed `MGC=F` bars:

```bash
python tools/download_mgc_yahoo.py --period 60d
python tools/test_mgc_dry_run.py
```

This evaluates the same signal state machine, dynamic dollar tier, stop
distance, and whole-contract minimum without credentials or order endpoints.
The Yahoo series is rolled/delayed and does not validate ProjectX execution,
slippage, or the actual Topstep historical feed.

### Historical decay comparison (Forex/XAUUSD reference only)

The table below is retained only as an old Forex/XAUUSD stress reference. It is
not an input to the MGC simulator and must not be combined with the MGC result.
It does not define the current simulator's result.

| Expectancy/trade | Pass | Fail |
|---|---|---|
| +0.236R (as measured) | 99.9% | 0.1% |
| +0.150R | 98.5% | 1.5% |
| +0.100R | 93.4% | 6.6% |
| +0.050R | 76.4% | **23.6%** |
| +0.020R | 56.0% | 44.0% |
| 0.000R | 41.0% | 59.0% |

Compare the same decay against FTMO, where +0.050R still passed 95.8% of the
time: **TopStep punishes a fading edge far harder**, and the trailing floor is
the reason. The system's margin of safety is much thinner here than the headline
99.9% suggests.

## What this simulation is not

1. **It is not a long-history acceptance benchmark.** Yahoo's `MGC=F` is a
   delayed, rolled series and the current cache covers only about 60 days. It
   has no Topstep/ProjectX spread or fill stream; costs are the configured
   commission/slippage estimates.
2. **Daily series, no intraday path.** The MLL is monitored continuously on
   realised *and* unrealised P&L, so a floor breach can happen inside a day
   that closes fine. This simulation only sees daily totals and is therefore
   optimistic about exactly the rule that kills accounts.
3. **The $400 internal stop is doing real work.** Removing it (firm rules only)
   drops the flat-regime pass rate at $200 risk from 97.2% to 94.4%, and at $300
   from 96.6% to 85.8%.
4. It quantifies path risk given the edge is real. It cannot validate the edge.

## Sources

- [Topstep Combine Rules 2026 — Tradecovex](https://tradecovex.com/guides/topstep-combine-rules-2026)
- [Topstep Rules Overview — PropTradingVibes](https://proptradingvibes.com/blog/topstep-rules-overview)
- [Topstep Combine Rules 2026: $50K/$100K/$150K Specs](https://proptradingvibes.com/blog/topstep-trading-combine-rules)
