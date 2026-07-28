# Generated output layout

```text
outputs/
├── backtests/
│   ├── technique_lab/<SYMBOL>/<TF>/report.json
│   ├── ai/cached/<SYMBOL>/<TF>/report.json
│   ├── ai/historical/<SYMBOL>/<TF>/report.json
│   └── quantum/summary.json
└── charts/
    ├── analysis/
    └── ftmo/
        ├── summary/
        └── symbols/<SYMBOL>/<TF>/performance.png
```

Files below `outputs` are generated artifacts. The authoritative FTMO summary
is written to `docs/FTMO_BACKTEST_SUMMARY.md`.
