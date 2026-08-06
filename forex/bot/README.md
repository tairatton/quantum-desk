# Quantum Bot — วิธีรันและผล Backtest

เอกสารนี้อ้างอิงโครงสร้างไฟล์และผล backtest ณ วันที่ 27 กรกฎาคม 2026

เอกสารตรวจความพร้อมก่อน production, ตารางผล, เทคนิค และโอกาสสอบผ่าน:
[PRODUCTION_READINESS.md](PRODUCTION_READINESS.md)

## โครงสร้าง

```text
bot/
├── broker.py, guardrails.py, run.py, trader.py
├── settings.py
├── repair_risk_cash.py
└── README.md   # เอกสารนี้

entrypoints/
├── main.py     # เมนู Forex
├── main.bat    # ดับเบิลคลิกเพื่อเริ่ม live
└── research.py # CLI วิเคราะห์/backtest
```

ไฟล์สำคัญภายใน `forex/bot`:

| ไฟล์ | หน้าที่ |
|---|---|
| `entrypoints/main.py` | เมนู Forex |
| `run.py` | ลูปเทรดและคำสั่ง CLI |
| `settings.py` | ค่า default และตำแหน่งไฟล์ runtime |
| `settings.local.json` | ค่าที่ใช้ทับ default ในเครื่องนี้ |
| `state.json` | สถานะที่ต้องรอดหลังปิดโปรแกรม |
| `journal.jsonl` | ประวัติการทำงาน/ผลเทรดจริง สร้างเมื่อมีข้อมูล |
| `news_cache.json` | cache ปฏิทินข่าว |
| `STOP` | ถ้ามีไฟล์นี้ บอทจะไม่เปิดไม้ใหม่ |

## เตรียมก่อนรัน

1. เปิด MetaTrader 5 และ login บัญชีที่ต้องการ
2. เปิด `Tools > Options > Expert Advisors > Allow algorithmic trading`
3. เปิด PowerShell ที่โฟลเดอร์หลัก `quantum-desk`
4. ติดตั้ง dependency จาก `requirements.txt` หากยังไม่ได้ติดตั้ง
5. ตรวจ `bot/settings.local.json` โดยเฉพาะ timeframe, risk, เวลา server และวันหยุดตลาด

ค่าปัจจุบันคือ XAUUSD, M15 + M30 และ Dynamic Risk ตาม drawdown:
1.00% → 0.75% → 0.50% → 0.40% โดยมี open/projected-day cap 1.50%
และ `capital_tier`: ต่ำกว่า $30,000 ใช้ Fixed TP3; ตั้งแต่ $30,000 ใช้ 33/33/34 + BE

## วิธีรัน

> **คำเตือน:** `entrypoints/main.bat` เริ่ม **live trading ทันที** ส่วน `entrypoints/main.py` เป็นเมนูที่มีคำยืนยันก่อนส่งออเดอร์จริง

กด **Run Python File** ที่ `entrypoints/main.py` ใน VS Code ได้เลย หรือดับเบิลคลิก:

```text
entrypoints\main.bat
```

รันจาก PowerShell ได้เช่นกัน:

```powershell
python -m entrypoints.main
```

เมื่อเริ่มแล้ว โปรแกรมจะเชื่อมต่อ MT5, แสดงสถานะบัญชี และเข้า live loop ทันที กด `Ctrl+C` เพื่อหยุดโปรแกรม Position ที่เปิดไปแล้วจะยังมี SL/TP ซึ่งฝากไว้ที่โบรกเกอร์

Terminal จะแสดง STARTING, WAITING, LIVE, เวลา server และเวลาตรวจแท่งถัดไป ถ้า MT5 หรือ Algo Trading ยังไม่พร้อม โปรแกรมจะรอและลองเชื่อมต่อใหม่ทุก 10 วินาที

### คำสั่งตรวจสอบและฉุกเฉิน

คำสั่งเหล่านี้ใช้แยกต่างหากเมื่อต้องการตรวจระบบโดยไม่เปิด launcher live:

```powershell
python -m bot.run --status
python -m bot.run --once
python -m bot.run --reconcile          # dry-run: inspect startup recovery only
python -m bot.run --reconcile --live   # persist recovery; no signal evaluation
python -m bot.run
python -m bot.run --flatten --live
```

- `--status` อ่านสถานะ แต่ต้องเชื่อมต่อ MT5
- `--once` เป็น dry-run หนึ่งรอบ
- `--reconcile` ตรวจและกู้ state/order/position ตอน startup แล้วจบ โดยไม่ประเมินสัญญาณ
- `--reconcile --live` บันทึกผล reconciliation จริง แต่ไม่เปิดออเดอร์ใหม่
- ไม่ใส่ flag เป็น dry-run แบบวนลูป
- `--flatten --live` ยกเลิก order และปิด position ของบอท เป็นคำสั่งฉุกเฉิน

ทุกช่องทางที่ใช้ `--live` ใช้ instance lock เดียวกัน จึงไม่สามารถเปิดสอง
process เพื่อส่งออเดอร์พร้อมกันได้ ส่วน dry-run ใช้ state ในหน่วยความจำและไม่
เขียนทับ `state.json` ของ production

## Terminal status และ notifications

ข้อความระหว่างรันเป็น English ทั้งหมดและใช้ event label คงที่:

| Event | ความหมาย |
|---|---|
| `[STATUS]` | การเริ่ม, รอ หรือหยุด MT5 connection |
| `[ACCOUNT]`, `[STRATEGY]`, `[RISK]` | สรุปบัญชี แผน และ risk ตอนเริ่ม |
| `EXPOSURE` section | จำนวน position, pending order และ floating P/L ใน startup dashboard |
| `[POSITION]` | รายละเอียด position ที่ยังเปิด: ticket, side, volume, entry, SL, TP, P/L |
| `[POSITION_HEALTH]` | plan/TF/exit role, กำไรเป็น R, ระยะถึง SL/TP และ protection alerts |
| `[PENDING]` | รายละเอียด pending order: ticket, type, volume, entry, SL, TP, expiry |
| `[PENDING_HEALTH]` | setup เจ้าของ order และเวลาที่เหลือก่อน expiry |
| `[ENTRY_CAPACITY]` | `YES`/`NO`/`CONDITIONAL` จาก slot, risk, request และ news gate |
| `[CONNECTION_LOST]`, `[RECONNECT_WAIT]` | เน็ตหรือ MT5 หลุดและกำลัง backoff |
| `[CONNECTION_RESTORED]` | ต่อ MT5 กลับแล้วและเริ่ม reconcile |
| `[HEARTBEAT]` | เวลา server, รอบตรวจถัดไป และ exposure ปัจจุบัน |
| `[ORDER_SUBMITTED]` | MT5 รับคำสั่งส่ง order แล้ว |
| `[POSITION_OPENED]` | market entry เปิด position สำเร็จ |
| `[PENDING_CREATED]` | สร้าง limit orders สำเร็จ |
| `[PENDING_FILLED]` | pending order ถูก fill และ map ไป position ticket แล้ว |
| `[STOP_MOVED]` | เลื่อน SL เช่น break-even |
| `[CLOSE_SUBMITTED]` | ส่งคำสั่งปิด position แล้ว แต่ยังรอผลยืนยัน |
| `[POSITION_CLOSED]` | ยืนยันว่าปิดแล้ว พร้อม net P/L, R และ costs |
| `[PENDING_CANCELLED]`, `[PENDING_REMOVED]` | pending ถูกยกเลิก, หมดอายุ หรือถูกปฏิเสธ |
| `[ALERT] UNTRACKED_*` | MT5 มี position/order ของบอท แต่ state ไม่ได้ manage ต้องตรวจทันที |
| `[RECONCILE_WAIT]`, `[PENDING_SYNC_WAIT]` | MT5 history ยังมาไม่ครบ บอทจะรอแทนการเดาผล |

ตัวอย่าง heartbeat:

```text
[HEARTBEAT] 2026-07-27 12:39:48 SERVER | LIVE | POS 0 | PENDING 0 | FLOAT +0.00 | NEXT 12:45:20 IN 00:05:32
```

Startup จะแสดง boxed dashboard แยก Account, Strategy and risk, Performance, Market and schedule,
News, Exposure และ Journal ส่วนรายละเอียด position/pending จะยังแสดงทุก heartbeat เมื่อมีรายการค้าง
## ตำแหน่งไฟล์หลังย้าย

`bot/settings.py` ใช้ตำแหน่งของตัวเองเป็นฐาน จึง resolve path เป็น:

| ตัวแปร | Path ปัจจุบัน |
|---|---|
| `BOT_DIR` | `forex/bot` |
| `STATE_PATH` | `bot/state.json` |
| `JOURNAL_PATH` | `bot/journal.jsonl` |
| `LOCAL_SETTINGS` | `bot/settings.local.json` |
| `KILL_SWITCH` | `bot/STOP` |
| `CACHE_PATH` | `bot/news_cache.json` |

ดังนั้น kill switch หลังย้ายต้องอยู่ที่ `bot/STOP` ไม่ใช่ `bot/STOP`

## ระบบที่บอทปัจจุบันรัน

รายละเอียด implementation, บัคที่แก้, จุดที่ยังต้องติดตาม และผลตรวจล่าสุดอยู่ใน
[CAPITAL_TIER_BUG_CHECK.md](CAPITAL_TIER_BUG_CHECK.md) และผลจำลองบัญชี $50K อยู่ใน
[FTMO_50K_SIMULATION.md](FTMO_50K_SIMULATION.md)

- Symbol: XAUUSD
- Timeframe ที่เปิดใช้: **M15 + M30** (เพิ่ม M30 เมื่อ 28 ก.ค. 2026)
- Risk: Dynamic 1.00% / 0.75% / 0.50% / 0.40% ตาม drawdown จาก balance high-water
  และให้ setup ถัดไปใช้ tier ที่ต่ำกว่าซึ่งพอดีกับ room ที่เหลือได้ โดย cap รวมยัง 1.50%
- Exit policy: **capital tier ที่ $30,000 โดยยึด initial balance**
- บัญชีที่ผูกอยู่: **FTMO Demo $50,000** → อยู่ tier บน
- Exit จริงของบัญชีนี้: **แบ่ง 33/33/34 ปิดที่ TP1/TP2/TP3 + BE หลัง TP1 + เลื่อน TP3 ไป TP1 หลัง TP2**
- `entrypoints/main.bat` เริ่ม live trading ทันที

### Exit ที่บอทรันจริงคือ BE + 33/33/34

`settings.local.json` ตั้ง `initial_balance: 50000.0` ซึ่งอยู่เหนือ
`split_exit_min_balance: 30000.0` บอทจึงใช้ **`be_after_tp1_33_33_34`**
และแบ่งไม้ได้จริงสามขา ไม่ตกไปเป็น leg เดียว — journal ยืนยันแล้ว:

```json
{"event": "trade_opened", "legs": [0.02, 0.02, 0.03],
 "single_leg": false, "exit_mode": "be_33_33_34"}
```

ที่ $50,000 งบเสี่ยงต่อไม้คือ **$200–$500 ตาม tier** และ SL ของทองอยู่แถว
$10–14 จึงแบ่งสามขาตาม `sizing._split` ได้
วัดจากข้อมูลย้อนหลัง **99% ของ setup แบ่งสามขาได้**

**ตัวเลข backtest ที่ต้องใช้อ้างอิงสำหรับบัญชีนี้คือของ `be_after_tp1_33_33_34`
ไม่ใช่ของ `fixed_tp3`:**

| | holdout net R | holdout expectancy | holdout DD |
|---|---:|---:|---:|
| XAUUSD M15 `be_after_tp1_33_33_34` | +43.7R | +0.1761R/trade | 9.06R |
| XAUUSD M30 `be_after_tp1_33_33_34` | +81.1R | +0.3572R/trade | 6.09R |

> **หมายเหตุประวัติ:** เอกสารรุ่นก่อนบรรยายเคสบัญชี **$10,000** ซึ่งอยู่ tier ล่าง
> และใช้ `fixed_tp3` ขาเดียว เพราะงบเสี่ยง $40 แบ่งสามขาไม่ได้ (0.01 lot ของทอง
> เสี่ยง $21–31 เกิน $13.33 ที่แบ่งได้) คำอธิบายนั้นยังถูกต้องสำหรับบัญชี
> < $30,000 แต่**ไม่ใช่บัญชีที่รันอยู่ตอนนี้** อย่านำตาราง `fixed_tp3`
> (+34.4R / +60.9R) มาใช้กับบัญชี $50K

### Risk จริงหลังปัดตาม lot step

บอทใช้ `nearest` แต่ยอมปัดขึ้นได้ไม่เกิน 15% จาก risk ของ tier ปัจจุบัน
โดยไม่ปล่อยให้ lot step ดันความเสี่ยงสูงเกินควบคุม ตัวเลขขึ้นกับระยะ SL ของแต่ละ setup
และ `--status` จะแสดงค่าประเมินล่าสุด เช่น:

```text
Strategy Risk/trade 1.00%   DD 0.00%   Cap 1.50%
Sizing   asked 1.00%   M15 ...   M30 ...
```

ค่าจริงเปลี่ยนตาม DD tier และระยะ stop ของ setup นั้น ค่า `risk_cash` และผล R ใน
journal คำนวณจาก lot ที่ส่งจริง ไม่ใช่งบ risk ที่ร้องขอ

### Capital-tier exit

โหมดถูกเลือกจาก `state.initial_balance` ที่บันทึกตอนเปิดบัญชีครั้งแรก ไม่ใช้ balance/equity
ปัจจุบัน จึงไม่สลับระบบกลางทางเมื่อกำไรทำให้ยอดข้าม $30,000:

| Initial balance | โหมดที่ใช้ |
|---|---|
| ต่ำกว่า $30,000 | `fixed_tp3`: position เดียว ปิดทั้งหมดที่ +2R ไม่เลื่อน SL |
| ตั้งแต่ $30,000 | `be_33_33_34`: TP1 +1R, TP2 +1.5R, TP3 +2R; หลัง TP1 เลื่อน TP2/TP3 ไป BE และหลัง TP2 เลื่อน TP3 ไปล็อกที่ TP1 |

สำหรับ XAU จุด BE/ขั้นกำไรจะบวกต้นทุนคอมมิชชันประมาณ $0.07 และ reserve
สำหรับการไถลของ stop $0.50 รวมเป็นประมาณ $0.57 เหนือราคาเปิดสำหรับ BUY
(หรือต่ำกว่าสำหรับ SELL) รวมทั้งชดเชย swap ติดลบที่เกิดขึ้นแล้วด้วย ค่า reserve
นี้ครอบคลุมการไถล $0.43 ที่พบเมื่อ 5 ส.ค. 2026 แต่ไม่สามารถรับประกันกรณีตลาด gap
หรือสภาพคล่องผิดปกติที่ไถลเกิน $0.50 ได้

```text
Exit    capital_tier $30,000 -> fixed_tp3 (one leg to TP3 (2R))
```

ถ้าอยู่ tier แบ่ง TP แต่ setup นั้น SL กว้างจนรวม lot แบ่งขั้นต่ำ 0.01 ได้ไม่ครบสามส่วน
บอทจะปฏิเสธ setup ทั้งหมด ไม่ลดเหลือ Fixed TP3 เฉพาะไม้นั้น เพราะจะเปลี่ยนนโยบายโดยเงียบ

## ผล Backtest ของระบบเดียวกับบอท

ตัวเลขด้านล่างมาจาก:

- `test/forex/outputs/backtests/technique_lab/XAUUSD/M15/report.json`
- `test/forex/outputs/backtests/technique_lab/XAUUSD/M30/report.json`

ใช้ข้อมูล 50,000 bars แบ่งตามเวลาเป็น train 60%, validation 20% และ locked holdout 20%
พร้อม purge 141 bars ระหว่างชุด ตัวเลขด้านล่างเป็น **`be_after_tp1_33_33_34`** ซึ่งเป็น
exit ที่บัญชี $50K ใช้จริง และรวมผลของ `CONVERT_TO_MARKET_BARS = 2` แล้ว
(รายงานเมื่อ 31 ก.ค. 2026 — ดู `docs/ENTRY_TIMING_EXPERIMENT_2026-07-31.md`)

### XAUUSD M15 — BE + 33/33/34 + TP2 step

ช่วงข้อมูล 12 มิถุนายน 2024 ถึง 24 กรกฎาคม 2026

| Split | Trades | Win rate | Net | Expectancy | PF | Max DD |
|---|---:|---:|---:|---:|---:|---:|
| Train | 752 | 48.14% | +129.39R | +0.1721R/trade | 1.452 | 10.97R |
| Validation | 226 | 54.42% | +74.07R | +0.3277R/trade | 2.009 | 5.62R |
| Holdout | 248 | 47.98% | +43.67R | +0.1761R/trade | 1.485 | 9.06R |

สรุป: M15 เป็นบวกทั้งสาม split แต่ช่วงข้อมูลเริ่มกลางปี 2024 จึงครอบคลุม regime
น้อยกว่า M30 และไม่ควรตีความว่า expectancy ระดับนี้จะคงอยู่ตลอด

> **เรื่อง max DD ที่ดูเหมือนแย่ลง:** holdout DD ขยับจาก 5.61R เป็น 9.06R ซึ่ง
> **ไม่ใช่ผลของ conversion** วัดด้วยการสลับลำดับไม้ชุดเดิม 2,000 ครั้ง: DD ที่เกิดจาก
> ดวงล้วน ๆ อยู่ในช่วง 5.07–11.55R (ก่อน) และ 5.84–13.65R (หลัง) — ทับกันเกือบหมด
> ค่า 5.61R เดิมอยู่ที่เปอร์เซ็นไทล์ 87 คือเป็น**ลำดับที่โชคดีผิดปกติ** ส่วน 9.06R
> อยู่ที่ 40 คือค่าปกติ ห้ามใช้ตัวเลขนี้ตัดสินใจอะไร

### XAUUSD M30 — BE + 33/33/34 + TP2 step

ช่วงข้อมูล 2 พฤษภาคม 2022 ถึง 24 กรกฎาคม 2026

| Split | Trades | Win rate | Net | Expectancy | PF | Max DD |
|---|---:|---:|---:|---:|---:|---:|
| Train | 693 | 46.75% | +36.94R | +0.0533R/trade | 1.116 | 25.78R |
| Validation | 225 | 52.44% | +46.88R | +0.2084R/trade | 1.504 | 14.04R |
| Holdout | 227 | 57.71% | +81.09R | +0.3572R/trade | 2.068 | 6.09R |

สรุป: M30 เป็นบวกทั้งสาม split แต่ train มี edge บางและ drawdown สูง แสดงว่าระบบเคยผ่าน
ช่วงตลาดที่ยากกว่าช่วง holdout ปัจจุบัน — holdout ของ exit แบบขั้นบันไดอยู่ที่ +81.09R
หลังเปิด conversion

## ความต่างจากรายงานวิจัยที่เลือก Exit

`docs/FTMO_BACKTEST_SUMMARY.md` เลือก exit จาก validation เพื่อไม่ใช้ holdout ทั้งเลือกและให้คะแนน

หลังเปิด `CONVERT_TO_MARKET_BARS = 2` การเลือกจาก validation ยังต่างจาก
production policy สำหรับ M15:

| | exit ที่ validation เลือก | ตรงกับที่บัญชี $50K รันไหม |
|---|---|---|
| XAUUSD M30 | **`be_after_tp1_33_33_34`** | ✅ ตรง (เดิมเลือก `fixed_tp3`) |
| XAUUSD M15 | `fixed_tp3` | ℹ️ production ตั้งใจรัน be33 ตาม capital tier |

บัญชีที่ initial balance ตั้งแต่ $30,000 ใช้ผล `be_after_tp1_33_33_34` เป็นตัวอ้างอิงเสมอ
ไม่ว่ารายงานจะเลือกอะไร เพราะเป็น production contract ที่เลือกจากความทนทานของ
หลาย split และ drawdown ไม่ใช่ validation ranking ของ TF เดียว ดังนั้น status จะแสดง
`research baseline` เป็นข้อมูลประกอบ ไม่ใช่ `CHECK` เมื่อใช้ `capital_tier`.

## ต้นทุนและข้อจำกัดของ Backtest

- spread มาจากข้อมูลราคา
- commission และ slippage ใช้ค่าประมาณจาก `strategy/config.py` ไม่ใช่ fill จริงของบัญชีนี้
- ข้อมูลราคามาจาก Exness แต่บัญชีเป้าหมายเป็น FTMO จึงมี spread และเวลา server ต่างกัน
- backtest ไม่รับประกันว่าจะผ่าน FTMO หรือทำกำไรในอนาคต
- news blackout และ weekend gap ของการรันจริงอาจลดจำนวน trade เมื่อเทียบกับ backtest
- ต้องเทียบ journal จริงอย่างน้อย 50 trades กับ holdout ก่อนสรุปว่า execution ยังรักษา edge ได้

## ตรวจระบบหลังแก้โค้ด

รัน unit tests โดยไม่เปิด live trading:

```powershell
python -B -m unittest discover -s tests -q
```

ตรวจ syntax/import ของ launcher โดยไม่เรียก `main()`:

```powershell
python -B -c "import entrypoints.main; import bot.run; print('imports OK')"
```

ห้ามรัน `entrypoints/main.bat` ระหว่างการทดสอบทั่วไป เพราะไฟล์นี้เปิด live ทันที

---

## เอกสารรวม: สถานะ production, Capital Tier และผลจำลอง FTMO

> ส่วนนี้รวบรวมสาระจากเอกสารการเตรียม production, การตรวจ Capital Tier,
> และแบบจำลอง FTMO $50K ไว้ในที่เดียว เพื่อให้ `bot/README.md` เป็นเอกสาร
> Markdown เพียงไฟล์เดียวของบอท

### สถานะการใช้งาน

Execution layer ออกแบบให้ใช้กับ MT5 ได้ แต่การเปิดใช้งานจริงเป็น **conditional go**:

- ต้องสร้าง virtual environment จาก `requirements.txt` และรัน unit tests ให้ผ่าน
- MT5 ต้องล็อกอินบัญชีที่ถูกต้องและเปิด Algorithmic Trading
- บัญชี MT5 ต้องเป็น **hedging mode**; บัญชี netting รวมขา TP1/TP2/TP3 เป็น position เดียว บอทจึงตรวจและหยุดก่อนเริ่มทำงาน
- เริ่มจาก `--status` และ `--once` (dry-run) เสมอ
- ต้องตรวจ log, lot, SL/TP, symbol, server clock และ news gate ก่อนใช้ `--live`
- ต้องเก็บผล forward test บน FTMO demo อย่างน้อย 50 closed trades ก่อนสรุปว่า edge จาก backtest ยังคงอยู่

### ผูกบัญชี MT5 กับบอท

บอทสามารถล็อกอินบัญชีที่กำหนดเองทุกครั้งที่เริ่มได้ผ่าน Windows User Environment Variables โดยไม่เก็บรหัสผ่านใน source หรือไฟล์ตั้งค่า:

```powershell
[Environment]::SetEnvironmentVariable('BOT_MT5_LOGIN', '<เลขบัญชี>', 'User')
[Environment]::SetEnvironmentVariable('BOT_MT5_PASSWORD', '<รหัสผ่าน>', 'User')
[Environment]::SetEnvironmentVariable('BOT_MT5_SERVER', '<ชื่อ server>', 'User')
```

ปิดแล้วเปิด PowerShell หรือ terminal ใหม่ก่อนรันบอท ค่าทั้งสามต้องมีครบพร้อมกัน; หากไม่ตั้งเลย บอทจะใช้บัญชีที่ล็อกอินอยู่ใน MT5 ตามปกติ. ห้ามบันทึกรหัสผ่านใน `settings.local.json` หรือ commit ลง Git

คำสั่งมาตรฐานสำหรับ production environment:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m bot.run --status
.\.venv\Scripts\python.exe -m bot.run --once
```

ใช้ `--live` เมื่อรายการตรวจทั้งหมดผ่านและผู้ดูแลยืนยันเท่านั้น ส่วน `--flatten --live` เป็นคำสั่งฉุกเฉินที่ปิด position/pending order จึงต้องใช้ด้วยความระมัดระวัง

### นโยบายกลยุทธ์และความเสี่ยง

บอทรัน XAUUSD บน M15 และ M30 โดยใช้แท่งที่ปิดแล้วเท่านั้น มี quality filter อย่างน้อย 6/9, จำกัด stop ที่ 0.8–2.5 ATR, และไม่เปิดฝั่งตรงข้ามกับ position ที่มีอยู่

| รายการ | ค่าเริ่มต้น |
|---|---:|
| Risk ต่อ setup | DD <0.50%: 1.00%; <1.00%: 0.75%; <1.50%: 0.50%; ที่เหลือ 0.40% |
| Open-risk / projected-day cap | 1.50% / 1.50% (รวม position และ pending) |
| Internal daily stop | 1.50% |
| FTMO daily / max loss guard | 5% / 10% |
| News blackout | USD high-impact −5 / +3 นาที |
| Pending expiry / active timeout | 2 แท่งเมื่อ conversion เปิด / 120 แท่ง |

Capital tier เป็นตัวกำหนด exit:

- Initial balance ต่ำกว่า $30,000: `fixed_tp3` — เปิดหนึ่ง position และปิดที่ TP3 (2R)
- Initial balance ตั้งแต่ $30,000: `be_after_tp1_33_33_34` — แบ่ง TP1/TP2/TP3 เป็น 33/33/34, เลื่อน TP2/TP3 ไป break-even หลัง TP1 และเลื่อน TP3 ไปล็อกกำไรระดับ TP1 หลัง TP2

อย่าแก้ exit ของ position ที่เปิดอยู่ย้อนหลัง และอย่าใช้ martingale, averaging down หรือขยาย SL; นโยบายเหล่านี้ถูกห้ามโดยการออกแบบ

### Position health, reconnect และ state

`--status` คือแหล่งข้อมูลจริงสำหรับ account, exposure, risk room, news cache และ position health. หากพบ `UNTRACKED`, `MISSING_SL/TP`, `ALERT` หรือ `CHECK SETTINGS` ต้องแก้ก่อนเปิด live ใหม่. โดยเฉพาะ `UNTRACKED` ที่มี position เปิดอยู่: อย่า archive/recreate state ซ้ำแล้วปล่อยบอททำงานต่อ เพราะ SL/TP ยังอยู่ที่โบรกเกอร์ก็จริง แต่การเลื่อน break-even และ active timeout จะไม่ถูกดูแล; ต้องตรวจและรับ position นั้นเข้า state ก่อน

State ถูกผูกกับ login/server เพื่อป้องกันนำ state จากบัญชีเดิมมาใช้ผิดบัญชี. เมื่อ MT5 หรือเครือข่ายหลุด บอทจะ reconnect แบบ backoff และ reconcile position/pending order; หาก TP1 ปิดระหว่างที่เครื่องหรือบอทหยุด การเปิดบอทครั้งถัดไปจะตรวจออเดอร์เดิมและเลื่อน SL ของ TP2/TP3 ไป break-even ทันทีใน startup sync. อย่างไรก็ดี SL/TP เท่านั้นที่อยู่ broker-side ส่วนการเลื่อน break-even เป็น client-side จึงควรปล่อย MT5 และบอททำงานต่อเนื่อง

บอทเก็บ audit log แบบ append-only ที่ `bot/journal.jsonl` โดยไม่บันทึกรหัสผ่าน. เหตุการณ์สำคัญ เช่น เริ่ม/หยุดบอท, ผูกบัญชี, startup sync, heartbeat, เปิด/filled/cancelled order, เลื่อน SL ไป break-even, guard block และการ reconnect จะถูกบันทึกไว้เพื่อย้อนตรวจภายหลัง. ดู 100 รายการล่าสุดได้ด้วย:

```powershell
Get-Content bot\code\journal.jsonl -Tail 100
```

ก่อนย้ายบัญชีหรือเริ่ม Challenge ใหม่ ให้ archive `state.json` และ `journal.jsonl`, ยืนยัน initial balance/phase ให้ตรงบัญชี และตรวจว่าเหลือ process บอทเพียงตัวเดียว

### FTMO $50K: ผลที่ใช้เป็นข้อมูลอ้างอิง

สำหรับ $50K บอทใช้ BE 33/33/34 ที่ risk 0.40% ต่อ setup. ผล holdout จากข้อมูล 50,000 bars มีดังนี้:

| Stream | Trades | Win rate | Expectancy | PF | Max DD ที่ risk 0.40% |
|---|---:|---:|---:|---:|---:|
| M15 BE33 | 248 | 47.98% | +0.1679R | 1.463 | 3.63% |
| M30 BE33 | 227 | 57.71% | +0.3437R | 2.027 | 2.44% |

แบบจำลอง Monte Carlo ใช้เพื่อวางแผน ไม่ใช่การรับประกันผลสอบ: เมื่อ edge เหลือ +0.05R ต่อ trade โอกาสผ่านสองขั้นจากแบบจำลองอยู่ที่ 90.95%; หาก edge เป็น 0R ลดเหลือ 19.74%. ความแตกต่างของ feed, spread, slippage, swap, ข่าว และ floating drawdown ทำให้ผลจริงต่างจาก backtest ได้

### Checklist ก่อน live

1. ตรวจ unit tests และ import ใน `.venv` ที่ใช้รันจริง
2. ตรวจ `--status`: login/server, symbol, risk, session clock, news, SL/TP และไม่มี alert
3. รัน `--once` แล้วอ่าน intent, lot, SL และ TP ที่ log แสดง
4. ปล่อย dry-run อย่างน้อยหนึ่งสัปดาห์ และเก็บ forward evidence ให้ครบอย่างน้อย 50 closed demo trades
5. ยืนยันกติกา FTMO ล่าสุด, ประเภทบัญชี Swing, phase และเวลาปิดตลาดก่อนเริ่ม Challenge
6. จัด watchdog/notification และปิด sleep/auto-restart เพื่อให้การดูแล break-even ทำงานต่อเนื่อง

### ข้อจำกัดที่ต้องยอมรับ

- Backtest และ Monte Carlo ไม่ยืนยันกำไรหรือการผ่าน FTMO
- News cache, holiday schedule และ weekly-close setting ต้องตรวจจาก `--status` เป็นระยะ
- บอทยังไม่รองรับหลาย symbol ใน process เดียว
- ไม่มี trailing stop นอกจาก BE หลัง TP1 และขั้น TP2 -> TP1 สำหรับ TP3
- หากเครื่องหรือบอทหยุดทำงาน การเลื่อน break-even จะหยุดตาม แต่ SL/TP ที่ส่งให้โบรกเกอร์ยังคงอยู่; เมื่อเปิดบอทใหม่ startup sync จะตรวจ TP1 และเลื่อนส่วนที่เหลือทันที
