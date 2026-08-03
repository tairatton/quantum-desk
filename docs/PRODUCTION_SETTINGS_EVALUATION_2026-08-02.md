# Production Settings Evaluation - 2026-08-02

## Scope

This document records the measurement and evaluation of the current production settings for the XAUUSD M15/M30 MT5 bot.

No live setting was changed during this evaluation. No new order was sent by the checks.

## Environment checks

- Python dependencies: `pip check` passed; no broken requirements.
- Python source: `compileall` passed.
- Automated tests: 271 tests passed.
- MT5 terminal: initialized and connected.
- MT5 permissions: terminal trading allowed, account trading allowed, and expert trading allowed.
- Account mode: Hedging.

## Current production settings

| Setting | Current value |
| --- | --- |
| Symbol / timeframes | XAUUSD / M15 + M30 |
| Initial balance | 50,000 |
| Base risk | 0.40% |
| Dynamic risk | Enabled; tiers 1.00% / 0.75% / 0.50% / floor 0.40% |
| Dynamic risk fitting | Enabled (`dynamic_risk_fit_remaining=true`) |
| Max open risk | 1.50% |
| Max risk per idea | 1.50% |
| Max concurrent trades | 2 |
| Internal daily stop | 1.50% |
| FTMO daily loss / max loss limits | 5% / 10% |
| Max consecutive losses | 3 |
| Exit mode | `capital_tier`; 50K tier uses BE 33/33/34 |
| News filter | Enabled; calendar required; USD high impact -5/+3 |

The live snapshot used for the operational check showed three protected positions, approximately 0.39% open risk, and three recorded trading days. The entry gate being blocked during the weekly close window is expected behavior, not a defect.

## Backtest aligned with the bot

The $50,000 `capital_tier` setting resolves to `BE + 33/33/34` for both production timeframes. The results below therefore use the same exit policy for XAUUSD M15 and M30.

| Timeframe | Split | Trades | Win rate | Net | Expectancy | PF | Max DD |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| M15 | Train | 752 | 48.14% | +120.89R | +0.1608R | 1.422 | 10.98R |
| M15 | Validation | 226 | 54.42% | +72.71R | +0.3217R | 1.991 | 5.62R |
| M15 | Holdout | 248 | 47.98% | +41.63R | +0.1679R | 1.463 | 9.06R |
| M30 | Train | 693 | 46.75% | +33.54R | +0.0484R | 1.106 | 26.80R |
| M30 | Validation | 225 | 52.44% | +45.18R | +0.2008R | 1.485 | 14.05R |
| M30 | Holdout | 227 | 57.71% | +78.03R | +0.3437R | 2.027 | 6.09R |

This removes the previous M15 policy mismatch. The separate research-selection table may still show Full TP3 for M15 because it answers a different question: which exit wins on validation. It must not be used as the production forecast for this $50,000 account.

## Old versus new comparison

The old column is the previous report's `Full TP3` result. The new column is the production-aligned `BE + 33/33/34` result. The trade counts and source report contents changed between runs, so the improvement cannot be attributed to the exit change alone.

| Timeframe | Metric | Old: Full TP3 | New: BE + 33/33/34 |
| --- | --- | ---: | ---: |
| M15 | Trades | 221 | 248 |
| M15 | Win rate | 29.41% | 47.98% |
| M15 | Net | +34.41R | +41.63R |
| M15 | Profit Factor | 1.36 | 1.463 |
| M15 | Max Drawdown | 8.25R | 9.06R |
| M30 | Trades | 204 | 227 |
| M30 | Win rate | 37.25% | 57.71% |
| M30 | Net | +60.92R | +78.03R |
| M30 | Profit Factor | 1.68 | 2.027 |
| M30 | Max Drawdown | 10.20R | 6.09R |

### Dynamic-risk comparison

The old mode blocked until the next full tier; the new mode uses `dynamic_risk_fit_remaining=true` to fit the next risk tier to the remaining room.

| Scenario | Old total days median / P90 | New total days median / P90 | Old 2-step pass | New 2-step pass | Breach old / new |
| --- | ---: | ---: | ---: | ---: | ---: |
| Holdout | 68 / 125 | 62 / 118 | 100.0% | 100.0% | 0.0% / 0.0% |
| Validation | 69 / 128 | 64 / 122 | 100.0% | 100.0% | 0.0% / 0.0% |
| Train | 161 / 335 | 158 / 334 | 94.9% | 94.8% | 1.1% / 1.1% |

The new mode is faster in the model but does not reduce modeled breach risk. The small Train pass-rate decrease should be treated as a speed-versus-risk-room trade-off.

### Forward comparison

There is no reliable old forward baseline. The new journal currently has four closed trades with combined net `+2.14R`; this is still too small for a meaningful comparison and should be reevaluated after at least 50 closed trades.

## Primary evaluation: dynamic-risk simulation

Command:

```text
python scripts/dynamic_risk_fit_sim.py --nsim 20000
```

The simulation is a conservative bootstrap of complete historical portfolio days. It models no more than two setups per day and reserves total risk at or below 1.50%. Results are conditional estimates, not a guarantee of passing an FTMO evaluation.

| Scenario | Risk mode | Step 1 pass | Breach | 2-step pass | Step 1 days median / P90 | Total days median / P90 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Holdout | Block | 100.0% | 0.0% | 100.0% | 42 / 88 | 68 / 125 |
| Holdout | Fit remaining | 100.0% | 0.0% | 100.0% | 38 / 85 | 62 / 118 |
| Validation | Block | 100.0% | 0.0% | 100.0% | 42 / 92 | 69 / 128 |
| Validation | Fit remaining | 100.0% | 0.0% | 100.0% | 39 / 88 | 64 / 122 |
| Train | Block | 96.3% | 1.1% | 94.9% | 98 / 242 | 161 / 335 |
| Train | Fit remaining | 96.1% | 1.1% | 94.8% | 94 / 243 | 158 / 334 |

### Interpretation

- `dynamic_risk_fit_remaining=true` reduces the modeled median time to target by about 4 days on holdout and 5 days on validation.
- It does not reduce the modeled breach probability: the train estimate remains 1.1%.
- On the train sample, fitting remaining capital slightly lowers the modeled two-step pass estimate from 94.9% to 94.8%. This is a small trade-off for faster modeled progress, not proof of a better strategy.
- The current 1.00% top tier is materially more aggressive than the 0.40% floor. It remains inside the configured 1.50% portfolio risk cap, but live execution must continue to enforce the cap across both timeframes.

## Forward evidence from the live journal

Command:

```text
python scripts/forward_check.py --journal bot/code/journal.jsonl
```

The journal currently contains only four closed trades, so this is an early signal and not enough to validate the setting.

| Timeframe | Trades | Net R | Mean R | Holdout expectancy | Cost / trade |
| --- | ---: | ---: | ---: | ---: | ---: |
| M15 | 2 | +1.11R | +0.5568R | +0.1679R | 0.0020R |
| M30 | 2 | +1.03R | +0.5151R | +0.3437R | 0.0415R |
| Combined | 4 | +2.14R | +0.5360R | Not stable at this sample size | N/A |

Forward sample status: 4 of the required 50 closed trades are available; 46 more are needed before treating the forward result as meaningful.

One M15 and one M30 record lack a matching `risk_cash` value. The checker handled these records, but the missing field should be monitored because it weakens later risk-normalized analysis.

## Fixed-risk reference check

The separate portfolio simulator was also run at fixed 0.40% and 1.00% risk as a reference. These figures should not be compared one-to-one with the dynamic-risk table because the simulators use different path and overlap assumptions.

Production-aligned portfolio command:

```text
python scripts/ftmo_portfolio_sim.py --book "XAU M15 + M30" --risk 0.40 --technique be_after_tp1_33_33_34 --nsim 20000
```

| Fixed risk | Scenario | Step 1 / breach / 2-step | Total days median / P90 | Worst drawdown |
| ---: | --- | ---: | ---: | ---: |
| 0.40% | Holdout | 100.0% / 0.0% / 100.0% | 55 / 79 | -1.70% |
| 0.40% | Validation | 100.0% / 0.0% / 100.0% | 45 / 62 | -1.63% |
| 0.40% | Train | 100.0% / 0.0% / 100.0% | 97 / 156 | -1.93% |
| 1.00% | Holdout | 99.9% / 0.1% / 99.8% | 23 / 38 | -4.26% |
| 1.00% | Validation | 100.0% / 0.0% / 100.0% | 19 / 30 | -4.09% |
| 1.00% | Train | 97.5% / 2.5% / 95.5% | 37 / 72 | -4.82% |

The reference supports the expected trade-off: higher fixed risk reaches the target faster but consumes considerably more drawdown room. The production dynamic-risk result is the primary decision basis.

## Assessment and precautions

### Assessment

The current setting is operationally ready and internally consistent with the tested risk limits. The new `fit remaining` behavior appears acceptable as a speed optimization because it shortens the modeled median path without increasing the modeled breach rate in these simulations. It should remain under observation rather than being declared proven.

### Precautions

- Keep MT5 Algo Trading enabled and verify the terminal is logged into the intended account before starting the live process.
- Do not judge the strategy from the current four-trade forward sample; continue until at least 50 closed trades, while also checking results by timeframe and regime.
- Monitor combined open risk across M15 and M30. The two timeframes can express correlated XAUUSD exposure even when they are separate trade ideas.
- Preserve `risk_cash` and close-event fields in the journal. Missing risk values reduce the quality of forward evaluation.
- Review behavior around weekly close/open, news blackout, and market holidays. These are intentional gates and can make the bot appear idle.
- The production-aligned backtest now uses BE 33/33/34 for both M15 and M30, matching the live `capital_tier` setting at the $50,000 initial balance.
- These estimates depend on historical samples, spread/slippage assumptions, and simulator assumptions. They are not a promise of passing or profitability.

## Files refreshed

- `docs/FTMO_BACKTEST_SUMMARY.md` was regenerated on 2026-08-02.
- The report now contains a production-aligned M15/M30 section pinned to `BE + 33/33/34`.
- This evaluation was added as `docs/PRODUCTION_SETTINGS_EVALUATION_2026-08-02.md`.
