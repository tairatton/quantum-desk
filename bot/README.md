# Quantum Bot — วิธีรันและผล Backtest

เอกสารนี้อ้างอิงโครงสร้างไฟล์และผล backtest ณ วันที่ 27 กรกฎาคม 2026

เอกสารตรวจความพร้อมก่อน production, ตารางผล, เทคนิค และโอกาสสอบผ่าน:
[PRODUCTION_READINESS.md](PRODUCTION_READINESS.md)

## โครงสร้าง

```text
bot/
├── code/       # โค้ด, settings, state, journal และ cache
├── main.py     # entry point แบบ Python
├── main.bat    # ดับเบิลคลิกเพื่อเริ่ม live ทันที
└── README.md   # เอกสารนี้
```

ไฟล์สำคัญภายใน `bot/code`:

| ไฟล์ | หน้าที่ |
|---|---|
| `main.py` | launcher ที่เริ่ม live trading ทันที |
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
5. ตรวจ `bot/code/settings.local.json` โดยเฉพาะ timeframe, risk, เวลา server และวันหยุดตลาด

ค่าปัจจุบันคือ XAUUSD, M15 + M30, risk 0.40% ของ balance เริ่มต้นต่อ trade
และ `capital_tier`: ต่ำกว่า $30,000 ใช้ Fixed TP3; ตั้งแต่ $30,000 ใช้ 33/33/34 + BE

## วิธีรัน

> **คำเตือน:** `bot/main.py` และ `bot/main.bat` เริ่ม **live trading ทันที** ไม่มีเมนูและไม่มีคำยืนยัน ออเดอร์สามารถถูกส่งเข้าบัญชีจริงได้

กด **Run Python File** ที่ `bot/main.py` ใน VS Code ได้เลย หรือดับเบิลคลิก:

```text
bot\main.bat
```

รันจาก PowerShell ได้เช่นกัน:

```powershell
python bot\main.py
```

เมื่อเริ่มแล้ว โปรแกรมจะเชื่อมต่อ MT5, แสดงสถานะบัญชี และเข้า live loop ทันที กด `Ctrl+C` เพื่อหยุดโปรแกรม Position ที่เปิดไปแล้วจะยังมี SL/TP ซึ่งฝากไว้ที่โบรกเกอร์

Terminal จะแสดง STARTING, WAITING, LIVE, เวลา server และเวลาตรวจแท่งถัดไป ถ้า MT5 หรือ Algo Trading ยังไม่พร้อม โปรแกรมจะรอและลองเชื่อมต่อใหม่ทุก 10 วินาที

### คำสั่งตรวจสอบและฉุกเฉิน

คำสั่งเหล่านี้ใช้แยกต่างหากเมื่อต้องการตรวจระบบโดยไม่เปิด launcher live:

```powershell
python -m bot.code.run --status
python -m bot.code.run --once
python -m bot.code.run
python -m bot.code.run --flatten --live
```

- `--status` อ่านสถานะ แต่ต้องเชื่อมต่อ MT5
- `--once` เป็น dry-run หนึ่งรอบ
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

`bot/code/settings.py` ใช้ตำแหน่งของตัวเองเป็นฐาน จึง resolve path เป็น:

| ตัวแปร | Path ปัจจุบัน |
|---|---|
| `BOT_DIR` | `bot/code` |
| `STATE_PATH` | `bot/code/state.json` |
| `JOURNAL_PATH` | `bot/code/journal.jsonl` |
| `LOCAL_SETTINGS` | `bot/code/settings.local.json` |
| `KILL_SWITCH` | `bot/code/STOP` |
| `CACHE_PATH` | `bot/code/news_cache.json` |

ดังนั้น kill switch หลังย้ายต้องอยู่ที่ `bot/code/STOP` ไม่ใช่ `bot/STOP`

## ระบบที่บอทปัจจุบันรัน

รายละเอียด implementation, บัคที่แก้, จุดที่ยังต้องติดตาม และผลตรวจล่าสุดอยู่ใน
[CAPITAL_TIER_BUG_CHECK.md](CAPITAL_TIER_BUG_CHECK.md) และผลจำลองบัญชี $50K อยู่ใน
[FTMO_50K_SIMULATION.md](FTMO_50K_SIMULATION.md)

- Symbol: XAUUSD
- Timeframe ที่เปิดใช้: **M15 + M30** (เพิ่ม M30 เมื่อ 28 ก.ค. 2026)
- Risk: 0.40% ของ initial balance ต่อ trade
- Exit policy: **capital tier ที่ $30,000 โดยยึด initial balance**
- Exit จริงบนบัญชี $10,000: **leg เดียว ปิดที่ TP3 (2R)**
- `bot/main.py` เริ่ม live trading ทันที

### ทำไม exit จึงไม่ใช่ BE + 33/33/34

เอกสารรุ่นก่อนเขียนว่าบอทใช้ BE + 33/33/34 ซึ่ง**ไม่ตรงกับสิ่งที่เกิดขึ้นจริง**
ระยะ stop ของทองโตขึ้นมากตามราคาและความผันผวน — วัดจากข้อมูล 12 เดือนล่าสุด
M15 อยู่ที่ **$20.96** และ M30 อยู่ที่ **$31.12** (ปี 2024 เคยอยู่แค่ $7–11)

งบเสี่ยงต่อไม้คือ 0.40% ของ $10,000 = **$40** จะแบ่งสาม leg ได้
ไม้เล็กสุดที่โบรกเกอร์ยอม (0.01 lot) ต้องเสี่ยงไม่เกิน **$13.33** แต่ทอง 1 จุดราคา
= $100 ต่อ lot ทำให้ 0.01 lot เสี่ยง $21–31 ซึ่งเกินไปแล้ว
`sizing._split` จึงคืน leg เดียว และ `single_leg_fallback_target = 2` ทำให้ปิดที่ TP3

**เรื่องนี้ไม่ใช่ปัญหา** เพราะ `fixed_tp3` คือ exit ที่งานวิจัยเลือกจาก validation
สำหรับ XAUUSD M15 และ M30 อยู่แล้ว (ดูหัวข้อ "ความต่างจากรายงานวิจัย" ด้านล่าง)
สิ่งที่บอททำจึง**ตรงกับ backtest ของ `fixed_tp3`** ไม่ใช่ของ BE + 33/33/34

| | holdout net R | ที่บอทรันจริง |
|---|---:|---|
| XAUUSD M15 `fixed_tp3` | +34.4R | ✅ ใช่ |
| XAUUSD M30 `fixed_tp3` | +60.9R | ✅ ใช่ |

### Risk จริงหลังปัดตาม lot step

บอทใช้ `nearest` แต่ยอมปัดขึ้นได้ไม่เกิน 15% จาก risk เป้าหมาย จึงพยายามเข้าใกล้
0.40% โดยไม่ปล่อยให้ lot step ดันความเสี่ยงสูงเกินควบคุม ตัวเลขขึ้นกับระยะ SL ของแต่ละ setup
และ `--status` จะแสดงค่าประเมินล่าสุด เช่น:

```text
Sizing   asked 0.40%   M15 0.02lot=0.41%   M30 0.01lot=0.31%!76%
```

M15 ตัวอย่างนี้อยู่ใกล้เป้าหมาย ส่วน M30 ต่ำกว่าเป้าเพราะ 0.02 lot จะเกินเพดาน overshoot
ค่า `risk_cash` และผล R ใน journal คำนวณจาก lot ที่ส่งจริง ไม่ใช่งบ risk ที่ร้องขอ

### Capital-tier exit

โหมดถูกเลือกจาก `state.initial_balance` ที่บันทึกตอนเปิดบัญชีครั้งแรก ไม่ใช้ balance/equity
ปัจจุบัน จึงไม่สลับระบบกลางทางเมื่อกำไรทำให้ยอดข้าม $30,000:

| Initial balance | โหมดที่ใช้ |
|---|---|
| ต่ำกว่า $30,000 | `fixed_tp3`: position เดียว ปิดทั้งหมดที่ +2R ไม่เลื่อน SL |
| ตั้งแต่ $30,000 | `be_33_33_34`: TP1 +1R, TP2 +1.5R, TP3 +2R; หลัง TP1 ปิดจริง เลื่อนสองส่วนที่เหลือไป BE |

```text
Exit    capital_tier $30,000 -> fixed_tp3 (one leg to TP3 (2R))
```

ถ้าอยู่ tier แบ่ง TP แต่ setup นั้น SL กว้างจนรวม lot แบ่งขั้นต่ำ 0.01 ได้ไม่ครบสามส่วน
บอทจะปฏิเสธ setup ทั้งหมด ไม่ลดเหลือ Fixed TP3 เฉพาะไม้นั้น เพราะจะเปลี่ยนนโยบายโดยเงียบ

## ผล Backtest ของระบบเดียวกับบอท

ตัวเลขด้านล่างมาจาก:

- `outputs/backtests/technique_lab/XAUUSD/M15/report.json`
- `outputs/backtests/technique_lab/XAUUSD/M30/report.json`

ใช้ข้อมูล 50,000 bars แบ่งตามเวลาเป็น train 60%, validation 20% และ locked holdout 20%
พร้อม purge 141 bars ระหว่างชุด ตัวเลขด้านล่างเป็น `fixed_tp3` สำหรับ tier ต่ำกว่า $30,000

### XAUUSD M15 — Fixed TP3

ช่วงข้อมูล 12 มิถุนายน 2024 ถึง 24 กรกฎาคม 2026

| Split | Trades | Win rate | Net | Expectancy | PF | Max DD |
|---|---:|---:|---:|---:|---:|---:|
| Train | 698 | — | +75.95R | +0.1088R/trade | 1.231 | 20.40R |
| Validation | 207 | — | +85.00R | +0.4106R/trade | 2.152 | 5.29R |
| Holdout | 221 | 29.41% | +34.41R | +0.1557R/trade | 1.364 | 8.25R |

สรุป: M15 เป็นบวกทั้งสาม split และ holdout ยังมีกำไร แต่ช่วงข้อมูลเริ่มกลางปี 2024
จึงครอบคลุม regime น้อยกว่า M30 และไม่ควรตีความว่า expectancy ระดับนี้จะคงอยู่ตลอด

### XAUUSD M30 — Fixed TP3

ช่วงข้อมูล 2 พฤษภาคม 2022 ถึง 24 กรกฎาคม 2026

| Split | Trades | Win rate | Net | Expectancy | PF | Max DD |
|---|---:|---:|---:|---:|---:|---:|
| Train | 649 | 29.28% | +1.92R | +0.0030R/trade | 1.005 | 44.92R |
| Validation | 200 | 37.00% | +51.03R | +0.2552R/trade | 1.534 | 14.67R |
| Holdout | 204 | 37.25% | +60.92R | +0.2986R/trade | 1.675 | 10.20R |

สรุป: M30 เป็นบวกทั้งสาม split แต่ train มี edge บางและ drawdown สูง แสดงว่าระบบเคยผ่าน
ช่วงตลาดที่ยากกว่าช่วง holdout ปัจจุบัน ปัจจุบันเปิดใช้ M30 ร่วมกับ M15 แล้ว

## ความต่างจากรายงานวิจัยที่เลือก Exit

`docs/FTMO_BACKTEST_SUMMARY.md` เลือก exit จาก validation เพื่อไม่ใช้ holdout ทั้งเลือกและให้คะแนน
ผลคือรายงานวิจัยเลือก `Full TP3` สำหรับ XAUUSD M15/M30 ไม่ใช่ `BE + 33/33/34`

ตัวอย่างผลที่รายงานเลือกสำหรับ XAUUSD M30 Full TP3:

| Split | Trades | Win rate | Net | Expectancy | PF | Max DD |
|---|---:|---:|---:|---:|---:|---:|
| Train | 649 | 29.28% | +1.92R | +0.0030R/trade | 1.005 | 44.92R |
| Validation | 200 | 37.00% | +51.03R | +0.2552R/trade | 1.534 | 14.67R |
| Holdout | 204 | 37.25% | +60.92R | +0.2986R/trade | 1.675 | 10.20R |

บัญชีที่ initial balance ตั้งแต่ $30,000 ใช้ผล `be_after_tp1_33_33_34` เป็นตัวอ้างอิงแทน
และ status จะแสดงคำเตือนถ้า technique ที่รายงานเลือกไม่ตรงกับ tier ที่กำลังทำงาน

## ต้นทุนและข้อจำกัดของ Backtest

- spread มาจากข้อมูลราคา
- commission และ slippage ใช้ค่าประมาณจาก `xau/config.py` ไม่ใช่ fill จริงของบัญชีนี้
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
python -B -c "import bot.main; import bot.code.run; print('imports OK')"
```

ห้ามรัน `bot/main.py` หรือ `bot/main.bat` ระหว่างการทดสอบทั่วไป เพราะทั้งสองตัวเปิด live ทันที

---

## เอกสารรวม: สถานะ production, Capital Tier และผลจำลอง FTMO

> ส่วนนี้รวบรวมสาระจากเอกสารการเตรียม production, การตรวจ Capital Tier,
> และแบบจำลอง FTMO $50K ไว้ในที่เดียว เพื่อให้ `bot/README.md` เป็นเอกสาร
> Markdown เพียงไฟล์เดียวของบอท

### สถานะการใช้งาน

Execution layer ออกแบบให้ใช้กับ MT5 ได้ แต่การเปิดใช้งานจริงเป็น **conditional go**:

- ต้องสร้าง virtual environment จาก `requirements-live.txt` และรัน unit tests ให้ผ่าน
- MT5 ต้องล็อกอินบัญชีที่ถูกต้องและเปิด Algorithmic Trading
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
.\.venv\Scripts\python.exe -m pip install -r requirements-live.txt
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m bot.code.run --status
.\.venv\Scripts\python.exe -m bot.code.run --once
```

ใช้ `--live` เมื่อรายการตรวจทั้งหมดผ่านและผู้ดูแลยืนยันเท่านั้น ส่วน `--flatten --live` เป็นคำสั่งฉุกเฉินที่ปิด position/pending order จึงต้องใช้ด้วยความระมัดระวัง

### นโยบายกลยุทธ์และความเสี่ยง

บอทรัน XAUUSD บน M15 และ M30 โดยใช้แท่งที่ปิดแล้วเท่านั้น มี quality filter อย่างน้อย 6/9, จำกัด stop ที่ 0.8–2.5 ATR, และไม่เปิดฝั่งตรงข้ามกับ position ที่มีอยู่

| รายการ | ค่าเริ่มต้น |
|---|---:|
| Risk ต่อ setup | 0.40% ของ initial balance |
| Open-risk cap | 0.80% |
| Internal daily stop | 1.50% |
| FTMO daily / max loss guard | 5% / 10% |
| News blackout | USD high-impact −5 / +3 นาที |
| Pending expiry / active timeout | 16 / 120 แท่ง |

Capital tier เป็นตัวกำหนด exit:

- Initial balance ต่ำกว่า $30,000: `fixed_tp3` — เปิดหนึ่ง position และปิดที่ TP3 (2R)
- Initial balance ตั้งแต่ $30,000: `be_after_tp1_33_33_34` — แบ่ง TP1/TP2/TP3 เป็น 33/33/34 และเลื่อนส่วนที่เหลือไป break-even หลัง TP1

อย่าแก้ exit ของ position ที่เปิดอยู่ย้อนหลัง และอย่าใช้ martingale, averaging down หรือขยาย SL; นโยบายเหล่านี้ถูกห้ามโดยการออกแบบ

### Position health, reconnect และ state

`--status` คือแหล่งข้อมูลจริงสำหรับ account, exposure, risk room, news cache และ position health. หากพบ `UNTRACKED`, `MISSING_SL/TP`, `ALERT` หรือ `CHECK SETTINGS` ต้องแก้ก่อนเปิด live ใหม่. โดยเฉพาะ `UNTRACKED` ที่มี position เปิดอยู่: อย่า archive/recreate state ซ้ำแล้วปล่อยบอททำงานต่อ เพราะ SL/TP ยังอยู่ที่โบรกเกอร์ก็จริง แต่การเลื่อน break-even และ active timeout จะไม่ถูกดูแล; ต้องตรวจและรับ position นั้นเข้า state ก่อน

State ถูกผูกกับ login/server เพื่อป้องกันนำ state จากบัญชีเดิมมาใช้ผิดบัญชี. เมื่อ MT5 หรือเครือข่ายหลุด บอทจะ reconnect แบบ backoff และ reconcile position/pending order; หาก TP1 ปิดระหว่างที่เครื่องหรือบอทหยุด การเปิดบอทครั้งถัดไปจะตรวจออเดอร์เดิมและเลื่อน SL ของ TP2/TP3 ไป break-even ทันทีใน startup sync. อย่างไรก็ดี SL/TP เท่านั้นที่อยู่ broker-side ส่วนการเลื่อน break-even เป็น client-side จึงควรปล่อย MT5 และบอททำงานต่อเนื่อง

บอทเก็บ audit log แบบ append-only ที่ `bot/code/journal.jsonl` โดยไม่บันทึกรหัสผ่าน. เหตุการณ์สำคัญ เช่น เริ่ม/หยุดบอท, ผูกบัญชี, startup sync, heartbeat, เปิด/filled/cancelled order, เลื่อน SL ไป break-even, guard block และการ reconnect จะถูกบันทึกไว้เพื่อย้อนตรวจภายหลัง. ดู 100 รายการล่าสุดได้ด้วย:

```powershell
Get-Content bot\code\journal.jsonl -Tail 100
```

ก่อนย้ายบัญชีหรือเริ่ม Challenge ใหม่ ให้ archive `state.json` และ `journal.jsonl`, ยืนยัน initial balance/phase ให้ตรงบัญชี และตรวจว่าเหลือ process บอทเพียงตัวเดียว

### FTMO $50K: ผลที่ใช้เป็นข้อมูลอ้างอิง

สำหรับ $50K บอทใช้ BE 33/33/34 ที่ risk 0.40% ต่อ setup. ผล holdout จากข้อมูล 50,000 bars มีดังนี้:

| Stream | Trades | Win rate | Expectancy | PF | Max DD ที่ risk 0.40% |
|---|---:|---:|---:|---:|---:|
| M15 BE33 | 221 | 46.61% | +0.1888R | 1.573 | 2.24% |
| M30 BE33 | 204 | 56.37% | +0.3142R | 1.917 | 1.97% |

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
- ไม่มี trailing stop นอกจาก break-even หลัง TP1
- หากเครื่องหรือบอทหยุดทำงาน การเลื่อน break-even จะหยุดตาม แต่ SL/TP ที่ส่งให้โบรกเกอร์ยังคงอยู่; เมื่อเปิดบอทใหม่ startup sync จะตรวจ TP1 และเลื่อนส่วนที่เหลือทันที
