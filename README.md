# HTF Quantum Adaptive — Multi-Asset

ระบบวิเคราะห์และสร้าง Trade Plan จาก Pine Script **HTF Quantum Adaptive Order Flow Entry TP/SL** บนข้อมูล MetaTrader 5

ระบบใช้ Pine HTF Quantum Adaptive เพียง engine เดียว โดยประกอบด้วย:

- Fractal BOS / CHoCH
- Adaptive Structure signal
- Balanced quality filter ผ่านอย่างน้อย 6 จาก 9 เงื่อนไข
- Adaptive entry: เข้าที่ close หรือรอ retracement 50%
- Stop Loss: structure + ATR buffer และจำกัดระยะ 0.8–2.5 ATR
- TP1 = 1R, TP2 = 1.5R, TP3 = 2R
- Session VWAP, CVD, POC, VAH และ VAL
- Win rate แยก TP1/TP2/TP3 ด้วยสูตรเดียวกับ Pine: `W / (W + L)`

## ติดตั้ง

```bash
python -m pip install -r requirements.txt
```

ต้องเปิด MetaTrader 5 และ login บัญชีไว้ก่อนใช้งาน

## Dashboard

```bash
python main.py serve --port 8050
```

เปิด `http://127.0.0.1:8050`

หน้า Dashboard แสดง:

- Market Bias และ Institutional Buying/Selling
- BOS / CHoCH บนกราฟ
- Entry, SL, TP1–TP3 และสถานะแผน
- Win rate TP1–TP3 พร้อมจำนวน W/L
- Adaptive filters ทั้ง 9 ข้อ
- VWAP, POC และ Value Area
- Filled / Expired / Cancelled / Timed out / Invalidated counters
- สลับ Price / Equity Curve ได้ทุก Symbol และ TF
- AI Trade Review ผ่าน OpenRouter แบบกดเรียกเอง

## OpenRouter AI Trade Review

AI เป็นตัวกรองแบบ advisory เท่านั้น: ยืนยันแผน Quantum หรือ block เป็น `WAIT`
AI ไม่มีสิทธิ์ส่งคำสั่ง MT5 และไม่สามารถเปิดฝั่งตรงข้ามเอง

ตั้งค่าใน Windows User Environment Variables แล้ว restart Dashboard:

```text
OPENROUTER_API_KEY=<สร้างจาก OpenRouter และห้ามใส่ใน source code>
OPENROUTER_MODEL=openai/gpt-5-mini
```

`OPENROUTER_MODEL` ไม่บังคับ หากไม่ตั้งจะใช้ `openai/gpt-5-mini`

- เรียก API เฉพาะเมื่อกด `วิเคราะห์ด้วย AI`
- ใช้ strict JSON Schema: `BUY / SELL / WAIT`, confidence, reasons และ risk flags
- ส่งเฉพาะข้อมูลตลาดที่ whitelist ไม่ส่งเลขบัญชีหรือชื่อ MT5 server
- บังคับ provider ที่รองรับ parameters และ `data_collection=deny`
- cache ตาม model, prompt version และข้อมูลแท่ง
- บันทึกที่ `data/ai_decisions/openrouter_decisions.jsonl` สำหรับ forward/AI backtest

AI backtest ต้องใช้ decision ที่บันทึกในเวลาจริงหรือ snapshot แบบ no-lookahead
ห้ามนำ AI มาวิเคราะห์อดีตโดยส่งสถิติจากอนาคตย้อนกลับไป เพราะจะทำให้ผลลวง

## CLI

```bash
python main.py analyze M15 --symbol BTCUSD --bars 6000
python main.py plot H1 --symbol XAUUSD
python main.py backtest
python main.py equity M15 --symbol BTCUSD --bars 6000
python main.py ai-backtest M15 --symbol BTCUSD --bars 6000
python main.py fetch M15 --symbol BTCUSD --bars 6000
python main.py symbols
```

Symbol: `XAUUSD`, `BTCUSD`, `EURUSD`, `GBPUSD`, `USDJPY`

Timeframe: `M1 M5 M15 M30 H1 H4 D1 W1`

## Technique Lab และรายงาน FTMO

Technique Lab เปรียบเทียบวิธีออกจากออเดอร์ด้วยสัญญาณเข้าเดียวกัน แบ่งข้อมูลตามเวลา
60% Train / 20% Validation / 20% Locked Holdout และเว้น purge 141 แท่ง:

```bash
python main.py technique-lab M30 --symbols EURUSD --bars 50000
python scripts/plot_ftmo_charts.py
python scripts/build_ftmo_report.py
python scripts/ftmo_portfolio_sim.py              # pass rate และจำนวนวันสอบ
python scripts/ftmo_portfolio_sim.py --by-year    # expectancy รายปี (regime risk)
```

`ftmo_portfolio_sim.py` รวมทุก stream เป็นบัญชีเดียวแล้วจำลองกฎ FTMO 2-Step
(target 10% / 5%, daily loss 5%, max loss 10%) พร้อมหัก commission และ slippage
ที่ technique lab ยังไม่ได้หัก ผลอยู่ที่ `docs/FTMO_SYMBOL_AND_TIMELINE.md`

กราฟ FTMO ใช้ Risk เริ่มต้น 0.25% ต่อไม้ เปลี่ยนได้ด้วย
`--risk-percent` กราฟราย Symbol อยู่ที่
`outputs/charts/ftmo/symbols/<SYMBOL>/<TF>/` และกราฟเปรียบเทียบอยู่ที่
`outputs/charts/ftmo/summary/` ส่วนรายงานสรุปอยู่ที่
`docs/FTMO_BACKTEST_SUMMARY.md`

## รันบอทเทรดจริง

```bash
python -m bot.code.run --status   # ดูบัญชี guard และสถิติ R ที่ทำได้จริง
python -m bot.code.run --once     # เดินหนึ่งรอบแบบ dry-run
python -m bot.code.run --live     # ส่งออเดอร์จริง
```

`bot/` ใช้ `xau.quantum` ตัวเดียวกับ backtest แล้วเพิ่ม position sizing, guardrails ของ FTMO,
state ที่รอด restart และ journal — ค่าเริ่มต้นเป็น dry-run เสมอ รายละเอียดที่ [bot/code/README.md](bot/code/README.md)

## Pine defaults ที่พอร์ตมา

| การตั้งค่า | ค่า |
|---|---:|
| Signal mode | Adaptive Structure |
| Filter preset | Balanced |
| Setup window | 8 bars |
| Required filters | 6/9 |
| Entry mode | Adaptive |
| Immediate break distance | ≤ 0.75 ATR |
| Pending retracement | 50% |
| Pending expiry | 16 bars |
| Active timeout | 120 bars |
| Structure ATR buffer | 0.20 ATR |
| Stop bounds | 0.8–2.5 ATR |
| TP1 / TP2 / TP3 | 1R / 1.5R / 2R |

ตัวกรอง 9 ข้อ: confirmed HTF EMA, local EMA, directional candle body, volume, ADX, DI direction, session VWAP, CVD slope และ RSI direction

## วิธีนับ Win rate

แต่ละ TP ถูกวัดแยกกัน:

```text
TP Win rate = TP wins / (TP wins + TP losses) × 100
```

- เมื่อ SL ถูกแตะ TP ที่ยังไม่สำเร็จจะถูกนับเป็น loss
- หากแท่งเดียวแตะ SL และ TP จะให้ SL เกิดก่อนแบบ conservative
- Pending plan ที่ไม่ fill จะไม่นับเป็น trade
- Timeout และ unresolved target ไม่เข้าตัวหาร ตาม Pine ต้นฉบับ
- ผลเป็นสถิติย้อนหลัง ไม่ใช่การรับประกันผลในอนาคต

## Pine Script สำหรับดูบน TradingView

[pine/quantum_bot_mirror.pine](pine/quantum_bot_mirror.pine) พอร์ตกลับจาก `xau/quantum.py`
เพื่อให้เห็นสัญญาณเดียวกับที่บอทเห็นบน MT5 — ค่าคงที่และสูตรทั้ง 10 จุดตรวจแล้วว่าตรงกัน

วิธีใช้: เปิด TradingView → Pine Editor → วางโค้ด → Add to chart → ตั้งกราฟเป็น XAUUSD M15 หรือ M30

แสดง BOS/CHoCH, entry/SL/TP, ตารางคะแนน filter 9 ข้อ และสถานะแผนปัจจุบัน
ค่าเริ่มต้นเน้นเส้น TP3 เพราะเป็นจุดออกที่บอทใช้จริงบนบัญชี $10,000

**ข้อจำกัด:** volume ของ TradingView ไม่เท่ากับ tick_volume ของ MT5 ทำให้ filter
Volume และ CVD slope อาจต่างกัน สัญญาณจึงไม่ตรงกัน 100% · สคริปต์นี้เป็นตัวดูอย่างเดียว
ไม่มี position sizing, guardrails ของ FTMO หรือ news blackout ซึ่งอยู่ใน `bot/code/`

## โครงสร้างหลัก

```text
main.py
xau/
├── quantum.py          # Pine signal, trade-plan state machine และ Win rate
├── quantum_chart.py    # กราฟ BOS/CHoCH, profile levels และ TP/SL
├── technique_lab.py    # Train/validation/locked-holdout exit tests
├── backtest_reporting.py # report discovery, selected TF และ shared labels
├── service.py          # MT5 lock, cache และ API payload
├── ai_advisor.py       # OpenRouter schema, privacy, gate และ decision log
├── webapp.py           # Flask dashboard
├── mt5_source.py       # ดึงและสะสมข้อมูล MT5
├── indicators.py       # ATR, EMA, RSI และ indicator helpers
└── config.py           # symbol, timeframe และ paths
templates/index.html
bot/                        # ตัวรันเทรดจริง (dry-run เป็นค่าเริ่มต้น)
├── main.py                 # entry point ของเมนู
├── main.bat                # launcher สำหรับ Windows
├── README.md               # วิธีรัน, path และผล backtest
└── code/
    ├── run.py              # ลูปหลัก ทำงานเมื่อแท่งปิด
    ├── broker.py           # session MT5, ส่ง/แก้/ปิดออเดอร์
    ├── sizing.py           # risk % -> lot และแบ่ง leg 33/33/34
    ├── guardrails.py       # ด่าน FTMO + ด่านภายใน
    ├── signals.py          # สะพานไป xau.quantum
    ├── trader.py           # เปิด 3 leg, BE หลัง TP1, timeout
    ├── news.py             # ปฏิทินข่าว + หน้าต่าง blackout
    ├── state.py            # state.json รอด restart
    └── journal.py          # journal.jsonl + สรุปสถิติ R
scripts/
├── plot_ftmo_charts.py     # กราฟ FTMO แบบ % และ Timeframe matrix
├── build_ftmo_report.py    # สร้าง Markdown จาก JSON reports
└── ftmo_portfolio_sim.py   # จำลองกฎ FTMO ระดับพอร์ต + expectancy รายปี
data/
├── market/<SYMBOL>/        # CSV และ provenance แยกตาม TF
└── ai_decisions/           # Forward AI decision log
docs/
├── FTMO_BACKTEST_SUMMARY.md     # ผล technique lab ทุก symbol / TF
├── FTMO_SYMBOL_AND_TIMELINE.md  # เลือก symbol, risk และประมาณเวลาสอบ
├── FTMO_RULES.md                # เงื่อนไข FTMO ทุกข้อ และจุดที่โค้ดบังคับ
└── NEWS_GUARD.md                # ระบบกันเปิดออเดอร์ตอนข่าว
outputs/
├── backtests/
│   ├── technique_lab/<SYMBOL>/<TF>/report.json
│   ├── ai/<MODE>/<SYMBOL>/<TF>/report.json
│   └── quantum/summary.json
└── charts/
    ├── analysis/           # กราฟจาก CLI analyze/equity
    └── ftmo/
        ├── summary/        # ภาพรวมและ Timeframe matrix
        └── symbols/<SYMBOL>/<TF>/performance.png
```
