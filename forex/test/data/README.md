# Data layout

```text
data/
├── market/<SYMBOL>/<TF>.csv
├── market/<SYMBOL>/<TF>.meta.json
└── ai_decisions/*.jsonl
```

`market` contains reproducible MetaTrader 5 caches. Each metadata file records
the broker symbol, source, bar count, and date range. `ai_decisions` contains
forward decisions only and must not be reconstructed with future information.
