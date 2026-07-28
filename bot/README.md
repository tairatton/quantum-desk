# Quantum Bot — วิธีรันและผล Backtest

เอกสารนี้อ้างอิงโครงสร้างไฟล์และผล backtest ณ วันที่ 27 กรกฎาคม 2026

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

ค่าปัจจุบันของเครื่องนี้คือ XAUUSD, M15 เท่านั้น และ risk 0.40% ของ balance เริ่มต้นต่อ trade
M30 ถูกปิดไว้สำหรับบัญชี $10,000 เพราะขนาด lot อาจแบ่งเป็นสาม leg ไม่ได้

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
## Terminal status และ notifications

ข้อความระหว่างรันเป็น English ทั้งหมดและใช้ event label คงที่:

| Event | ความหมาย |
|---|---|
| `[STATUS]` | การเริ่ม, รอ หรือหยุด MT5 connection |
| `[ACCOUNT]`, `[STRATEGY]`, `[RISK]` | สรุปบัญชี แผน และ risk ตอนเริ่ม |
| `EXPOSURE` section | จำนวน position, pending order และ floating P/L ใน startup dashboard |
| `[POSITION]` | รายละเอียด position ที่ยังเปิด: ticket, side, volume, entry, SL, TP, P/L |
| `[PENDING]` | รายละเอียด pending order: ticket, type, volume, entry, SL, TP, expiry |
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

- Symbol: XAUUSD
- Timeframe ที่เปิดใช้: **M15 + M30** (เพิ่ม M30 เมื่อ 28 ก.ค. 2026)
- Risk: 0.40% ของ initial balance ต่อ trade
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

### ⚠️ risk จริงต่ำกว่าที่ตั้งไว้ เพราะ lot step ปัดลงได้เท่านั้น

บัญชี $10,000 ที่ risk 0.40% ต้องการ **0.0191 lot** แต่ step คือ 0.01 จึงส่งได้ **0.01 lot**
ซึ่งเสี่ยงจริงแค่ **$20.96 = 0.21%** ไม่ใช่ $40

| TF | lot ที่ควรใช้ | ส่งได้ | risk จริง | เทียบเป้า |
|---|---:|---:|---:|---:|
| M15 (SL $20.96) | 0.0191 | 0.01 | **0.21%** | 52% |
| M30 (SL $31.12) | 0.0129 | 0.01 | **0.31%** | 76% |

การปัด**ลง**เป็นเจตนา (`floor_to_step` — "do not raise the risk to fit") ปลอดภัยกว่า
แต่เดิม `Sizing.risk_cash` เก็บ**งบที่ตั้งใจ** ($40) ไม่ใช่ความเสี่ยงจริง ($20.96)
ทำให้ `reconcile_closed` คิด R ผิด — **โดน SL เต็มอ่านได้ −0.52R แทน −1.00R**
และ `expectancy_r` ใน journal สูงเกินจริงราว **1.9 เท่า**

แก้แล้ว: `risk_cash` คิดจาก lot ที่ส่งจริง · เพิ่ม `intended_risk_cash` และ
`risk_shortfall` ไว้ให้เห็นช่องว่าง · journal บันทึก `risk_rounded_down` เมื่อต่ำกว่า 85%
· `--status` แสดงบรรทัด `Sizing` พร้อมเครื่องหมาย `!52%`

```text
Sizing   asked 0.40%   M15 0.01lot=0.21%!52%   M30 0.01lot=0.31%!76%
```

**shortfall ไม่ได้ดีขึ้นเรื่อย ๆ ตามทุน — มันเป็นฟันเลื่อย** เพราะ 0.01 lot เป็นก้อนคงที่
ทุนที่อยู่ใต้ threshold ถัดไปพอดีจะแย่สุด และ $10,000 บังเอิญอยู่ใกล้จุดนั้น:

| ทุน | lot ที่ควรใช้ | ส่งได้ | risk จริง | shortfall |
|---:|---:|---:|---:|---:|
| $5,500 | 0.0105 | 0.01 | 0.381% | 95% |
| $8,000 | 0.0153 | 0.01 | 0.262% | 66% |
| **$10,000** | 0.0191 | 0.01 | **0.210%** | **52%** |
| **$10,500** | 0.0200 | **0.02** | **0.399%** | **100%** |
| $15,000 | 0.0286 | 0.02 | 0.279% | 70% |
| $21,000 | 0.0401 | 0.04 | 0.399% | 100% |

เพิ่มทุนแค่ $500 ทำให้ lot โตเท่าตัว · แอมพลิจูดของฟันเลื่อยแคบลงเมื่อทุนโตขึ้น
เพราะ 0.01 lot กลายเป็นสัดส่วนที่เล็กลง

**ผลต่อความคาดหวัง:** บอทเสี่ยงราว **ครึ่งเดียว** ของที่ตั้งไว้ จึงโตช้ากว่าที่ forecast
คำนวณไว้ (ซึ่งใช้ 0.40% เต็ม) ประมาณเท่าตัว แต่โอกาสล้มก็ต่ำกว่าเท่าตัวด้วย —
ไม่ใช่เรื่องเสียหาย แต่ต้องรู้ว่าเวลาถึงเป้าจะยาวกว่าตัวเลขใน forecast

### exit ถูกตรึงไว้แล้ว ไม่เปลี่ยนตามขนาดบัญชี

เดิม exit เป็น**ผลพลอยได้จากขนาด lot** — แบ่ง 3 leg เมื่อบัญชีใหญ่พอ และเหลือ leg เดียว
เมื่อไม่พอ แปลว่าพอบัญชีโตข้าม ~$16,000 (M15) หรือ ~$24,000 (M30) ระบบจะ**เปลี่ยนเป็น
BE + 33/33/34 เองโดยไม่มีใครกดอะไร** ซึ่งเป็นคนละระบบกับที่วัดไว้

ตอนนี้มี `exit_mode` ใน settings ให้ระบุตรงๆ:

| ค่า | ทำอะไร |
|---|---|
| **`fixed_tp3`** (ค่าที่ใช้อยู่) | leg เดียวปิดที่ TP3 เสมอ **ทุกขนาดบัญชี** |
| `be_33_33_34` | 3 legs 33/33/34 + เลื่อน BE หลัง TP1 · **ปฏิเสธไม้** ถ้าบัญชีแบ่งไม่ได้ แทนที่จะลดเหลือ leg เดียวเงียบๆ |
| `auto` | พฤติกรรมเดิม แบ่งเมื่อแบ่งได้ — ไม่แนะนำ |

พิสูจน์ว่าตรึงได้จริง (SL M15 $20.96, risk 0.40%):

| ยอดเงิน | `fixed_tp3` (ตรึง) | `auto` (เดิม) |
|---|---|---|
| $10,000 | `(0.01,)` TP3 | `(0.01,)` |
| **$16,000** | `(0.03,)` TP3 | **`(0.01,0.01,0.01)`** ← เดิมเปลี่ยนตรงนี้ |
| $50,000 | `(0.09,)` TP3 | `(0.03,0.03,0.03)` |
| $100,000 | `(0.19,)` TP3 | `(0.06,0.06,0.07)` |

**`--status` ตรวจให้ด้วย** ว่าชื่อ exit ยังตรงกับที่งานวิจัยเลือกไว้:

```text
Exit    fixed_tp3 — one leg to TP3 (2R)    matches the study
```

ถ้าคำนวณ backtest ใหม่แล้ว validation เลือกวิธีอื่น จะขึ้นเตือน:

```text
Exit    be_33_33_34 — ...    <-- CHECK: M15 wants fixed_tp3, M30 wants fixed_tp3
```

## ผล Backtest ของระบบเดียวกับบอท

ตัวเลขด้านล่างมาจาก:

- `outputs/backtests/technique_lab/XAUUSD/M15/report.json`
- `outputs/backtests/technique_lab/XAUUSD/M30/report.json`

ใช้ข้อมูล 50,000 bars แบ่งตามเวลาเป็น train 60%, validation 20% และ locked holdout 20%
พร้อม purge 141 bars ระหว่างชุด ตัวเลขเป็นหน่วย `R` และใช้ exit
`be_after_tp1_33_33_34` ซึ่งตรงกับบอทปัจจุบัน

### XAUUSD M15 — BE + 33/33/34

ช่วงข้อมูล 12 มิถุนายน 2024 ถึง 24 กรกฎาคม 2026

| Split | Trades | Win rate | Net | Expectancy | PF | Max DD |
|---|---:|---:|---:|---:|---:|---:|
| Train | 698 | 46.99% | +100.94R | +0.1446R/trade | 1.377 | 11.35R |
| Validation | 207 | 56.04% | +78.20R | +0.3778R/trade | 2.297 | 4.66R |
| Holdout | 221 | 46.61% | +41.72R | +0.1888R/trade | 1.573 | 5.61R |

สรุป: M15 เป็นบวกทั้งสาม split และ holdout ยังมีกำไร แต่ช่วงข้อมูลเริ่มกลางปี 2024
จึงครอบคลุม regime น้อยกว่า M30 และไม่ควรตีความว่า expectancy ระดับนี้จะคงอยู่ตลอด

### XAUUSD M30 — BE + 33/33/34

ช่วงข้อมูล 2 พฤษภาคม 2022 ถึง 24 กรกฎาคม 2026

| Split | Trades | Win rate | Net | Expectancy | PF | Max DD |
|---|---:|---:|---:|---:|---:|---:|
| Train | 649 | 47.15% | +36.50R | +0.0562R/trade | 1.126 | 24.09R |
| Validation | 200 | 52.00% | +47.23R | +0.2362R/trade | 1.599 | 14.99R |
| Holdout | 204 | 56.37% | +64.09R | +0.3142R/trade | 1.917 | 4.92R |

สรุป: M30 เป็นบวกทั้งสาม split แต่ train มี edge บางและ drawdown สูง แสดงว่าระบบเคยผ่าน
ช่วงตลาดที่ยากกว่าช่วง holdout ปัจจุบัน ทั้งนี้ M30 ยังไม่เปิดใช้ใน settings ของบัญชี $10,000

## ความต่างจากรายงานวิจัยที่เลือก Exit

`docs/FTMO_BACKTEST_SUMMARY.md` เลือก exit จาก validation เพื่อไม่ใช้ holdout ทั้งเลือกและให้คะแนน
ผลคือรายงานวิจัยเลือก `Full TP3` สำหรับ XAUUSD M15/M30 ไม่ใช่ `BE + 33/33/34`

ตัวอย่างผลที่รายงานเลือกสำหรับ XAUUSD M30 Full TP3:

| Split | Trades | Win rate | Net | Expectancy | PF | Max DD |
|---|---:|---:|---:|---:|---:|---:|
| Train | 649 | 29.28% | +1.92R | +0.0030R/trade | 1.005 | 44.92R |
| Validation | 200 | 37.00% | +51.03R | +0.2552R/trade | 1.534 | 14.67R |
| Holdout | 204 | 37.25% | +60.92R | +0.2986R/trade | 1.675 | 10.20R |

ดังนั้นห้ามนำผล Full TP3 ไปอ้างว่าเป็นผลของบอท live ปัจจุบัน เพราะบอทยังใช้
BE + 33/33/34 อยู่ หากจะเปลี่ยน exit ต้องแก้โค้ดและทดสอบใหม่ก่อน

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
