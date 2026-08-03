# FTMO Technique-Lab Backtest Summary

Generated: 2026-08-02

## Scope and method

- Deterministic HTF Quantum entries; AI/Elliott Wave is not used.
- Chronological split: 60% train, 20% validation, 20% locked holdout.
- A 141-bar purge separates the earlier splits from subsequent outcomes.
- **The exit technique is chosen on validation, never on the holdout.** Ranking by holdout net R let the holdout both pick the technique and score it; USDJPY M15 read +11.5R that way and -8.5R once chosen honestly.
- **Cost is spread + commission + slippage** (`xau.config.COSTS`). Bars are bid-quoted, so a round trip pays the spread once; commission and slippage are absent from the feed and are modelled per symbol. The figures are estimates, not measurements - replace them with what an FTMO demo actually charges.
- Every exit technique uses the same filled entries, so exit comparisons do not change trade frequency.

## Production-aligned results for the current bot

The live account starts at $50,000. Its `capital_tier` setting resolves to `be_after_tp1_33_33_34` for both XAUUSD M15 and M30. The tables below pin both timeframes to that exit instead of selecting an exit separately for research.

| Symbol | TF | Exit technique | Holdout trades | Win rate | Net | Expectancy | PF | Max DD |
|---|---:|---|---:|---:|---:|---:|---:|---:|
| XAUUSD | M15 | BE + 33/33/34 | 248 | 47.98% | +41.63R | +0.17R/trade | 1.46 | 9.06R |
| XAUUSD | M30 | BE + 33/33/34 | 227 | 57.71% | +78.03R | +0.34R/trade | 2.03 | 6.09R |

### Production-aligned split results

| Symbol | TF | Split | Trades | Win rate | Net | Expectancy | PF | Max DD |
|---|---:|---|---:|---:|---:|---:|---:|---:|
| XAUUSD | M15 | Train | 752 | 48.14% | +120.89R | +0.16R/trade | 1.42 | 10.97R |
| XAUUSD | M15 | Validation | 226 | 54.42% | +72.71R | +0.32R/trade | 1.99 | 5.62R |
| XAUUSD | M15 | Holdout | 248 | 47.98% | +41.63R | +0.17R/trade | 1.46 | 9.06R |
| XAUUSD | M30 | Train | 693 | 46.75% | +33.54R | +0.05R/trade | 1.11 | 26.80R |
| XAUUSD | M30 | Validation | 225 | 52.44% | +45.18R | +0.20R/trade | 1.49 | 14.04R |
| XAUUSD | M30 | Holdout | 227 | 57.71% | +78.03R | +0.34R/trade | 2.03 | 6.09R |

## Selected smallest practical profitable timeframe

| Symbol | TF | Exit technique | Holdout trades | Win rate | Net | PF | Max DD | Consistent? |
|---|---:|---|---:|---:|---:|---:|---:|---|
| XAUUSD | M30 | BE + 33/33/34 | 227 | 57.71% | +78.03R | 2.03 | 6.09R | Yes |
| EURUSD | M30 | BE + 33/33/34 | 215 | 52.09% | +26.04R | 1.29 | 10.51R | No |

The selection prioritises the smallest timeframe with a material edge and positive train, validation, and holdout splits. Tiny positive results with high drawdown are rejected. A row marked `Consistent? = No` is **not tradeable** - it is listed because it was a candidate, not because it passed.

### Considered and rejected

| Symbol | TF | Why |
|---|---:|---|
| USDJPY | M15 | holdout flips to -8.5R once the technique is picked on validation |
| GBPUSD | M15 | edge too thin to carry cost: 10.6% of R is spread alone |
| BTCUSD | M5 | only 5 weeks of holdout, and cost is 5-15% of R |

Charts assume 0.25% account risk per trade.

[Selected overview](../outputs/charts/ftmo/summary/overview.png) · [Split expectancy](../outputs/charts/ftmo/summary/split_expectancy.png) · [Timeframe matrix](../outputs/charts/ftmo/summary/timeframe_matrix.png)

## Selected split results

### XAUUSD M30

Selected exit: **BE + 33/33/34**

| Split | Trades | Win rate | Net | Expectancy | PF | Max DD |
|---|---:|---:|---:|---:|---:|---:|
| Train | 693 | 46.75% | +33.54R | +0.05R/trade | 1.11 | 26.80R |
| Validation | 225 | 52.44% | +45.18R | +0.20R/trade | 1.49 | 14.04R |
| Holdout | 227 | 57.71% | +78.03R | +0.34R/trade | 2.03 | 6.09R |

[FTMO performance chart](../outputs/charts/ftmo/symbols/XAUUSD/M30/performance.png)

### EURUSD M30

Selected exit: **BE + 33/33/34**

| Split | Trades | Win rate | Net | Expectancy | PF | Max DD |
|---|---:|---:|---:|---:|---:|---:|
| Train | 642 | 44.08% | -23.82R | -0.04R/trade | 0.93 | 48.71R |
| Validation | 205 | 47.80% | +11.73R | +0.06R/trade | 1.13 | 14.25R |
| Holdout | 215 | 52.09% | +26.04R | +0.12R/trade | 1.29 | 10.51R |

[FTMO performance chart](../outputs/charts/ftmo/symbols/EURUSD/M30/performance.png)

## All available technique-lab results

| Symbol | TF | Bars | Best exit | Holdout trades | Win rate | Net | PF | Max DD |
|---|---:|---:|---|---:|---:|---:|---:|---:|
| XAUUSD | M5 | 50000 | BE + 33/33/34 | 239 | 47.70% | +31.36R | 1.38 | 9.51R |
| XAUUSD | M15 | 50000 | Full TP3 | 248 | 30.65% | +38.11R | 1.34 | 11.42R |
| XAUUSD | M30 | 50000 | BE + 33/33/34 | 227 | 57.71% | +78.03R | 2.03 | 6.09R |
| XAUUSD | H1 | 50000 | Full TP3 | 236 | 37.29% | +49.50R | 1.39 | 9.24R |
| XAUUSD | H4 | 15962 | BE + 33/33/34 | 73 | 61.64% | +24.09R | 1.92 | 4.02R |
| BTCUSD | M5 | 50002 | BE + 33/33/34 | 238 | 43.70% | +23.53R | 1.25 | 10.65R |
| EURUSD | M5 | 50000 | BE + 33/33/34 | 239 | 41.00% | -51.31R | 0.62 | 54.09R |
| EURUSD | M15 | 50000 | BE + 33/33/34 | 230 | 46.96% | +19.17R | 1.21 | 10.60R |
| EURUSD | M30 | 49625 | BE + 33/33/34 | 215 | 52.09% | +26.04R | 1.29 | 10.51R |
| EURUSD | H1 | 24813 | Regime adaptive | 97 | 41.24% | +2.56R | 1.05 | 14.33R |
| EURUSD | H4 | 6415 | Full TP3 | 28 | 28.57% | -1.07R | 0.94 | 5.50R |
| GBPUSD | M5 | 50000 | BE + 33/33/34 | 222 | 43.24% | -29.63R | 0.74 | 34.51R |
| GBPUSD | M15 | 50000 | BE + 33/33/34 | 207 | 46.86% | +2.65R | 1.03 | 11.94R |
| GBPUSD | M30 | 49625 | BE + 33/33/34 | 215 | 48.84% | +5.94R | 1.06 | 11.07R |
| GBPUSD | H1 | 24813 | Full TP3 | 107 | 29.91% | -5.43R | 0.92 | 17.13R |
| GBPUSD | H4 | 6415 | BE + 33/33/34 | 33 | 36.36% | -3.40R | 0.82 | 7.02R |
| USDJPY | M5 | 50000 | BE + 33/33/34 | 233 | 37.77% | -56.28R | 0.58 | 58.32R |
| USDJPY | M15 | 50000 | Full TP3 | 199 | 24.12% | -28.21R | 0.76 | 32.97R |
| USDJPY | M30 | 49625 | BE + 33/33/34 | 190 | 48.95% | +13.75R | 1.16 | 9.88R |
| USDJPY | H1 | 24813 | BE + 33/33/34 | 98 | 48.98% | +7.06R | 1.17 | 6.30R |
| USDJPY | H4 | 6415 | Full TP1 | 23 | 43.48% | -2.71R | 0.78 | 6.33R |

## FTMO risk plan

- Preferred evaluation: FTMO 2-Step, because its max loss is static rather than trailing and this system's worst historical drawdown is 15-22R.
- **Gold only.** EURUSD was the intended half-risk helper and does not survive the cost model on any timeframe, so there is no second stream left to diversify into.
- Risk per trade: 0.25-0.40% of the **initial** balance, never the live balance.
- Aggregate open risk: maximum 0.80%, and no more than two positions at once, because gold M15 and M30 usually agree.
- Internal daily stop: -1.50%; stop after three consecutive losing trades; stop entirely once the target and the four trading days are both in.
- No martingale, grid recovery, averaging down, or widening a stop loss.
- Estimated duration, not a guarantee: 25-50 trading days for the 10% Challenge and 13-25 trading days for the 5% Verification. The news and market-close blackouts in `bot/` are wider than the rules require, so expect fewer trades than the study's 2.8/day and a correspondingly longer run.

Official rules must be checked again before purchase: [FTMO Trading Objectives](https://ftmo.com/en/trading-objectives/) and [FTMO 2-Step](https://ftmo.com/en/2-step-challenge/).

## Reproduce

```powershell
python scripts\plot_ftmo_charts.py
python scripts\build_ftmo_report.py
python -m unittest discover -s tests -q
```

## Limitations

Historical results do not guarantee an FTMO pass. The cost figures are estimates of the right order of magnitude, not measurements taken on the account that will be traded - measure them on an FTMO demo first, because the whole difference between an edge and no edge on the FX pairs was cost. Data comes from Exness, whose spreads and server clock both differ from FTMO's. Gold ran two losing years and one flat year out of nine, so a flat regime is a normal outcome rather than a tail. Nothing here simulates news or weekend gaps.
