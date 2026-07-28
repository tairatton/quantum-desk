# Quantum Desk — Production Readiness และกลยุทธ์ปัจจุบัน

อัปเดตล่าสุด: 28 กรกฎาคม 2026  
ขอบเขต: FTMO 2-Step Swing $50K, XAUUSD M15 + M30, risk 0.40% ต่อ setup

## คำตัดสิน

| มิติ | สถานะ | ผลปัจจุบัน |
|---|---|---|
| โค้ดและ unit tests | พร้อม | Python 3.13.14, tests 127/127, compile และ `git diff --check` ผ่าน |
| การเชื่อมต่อ MT5 | พร้อม | อ่าน account, symbol, position, pending, SL/TP และ reconnect ได้ |
| การป้องกันออเดอร์ซ้ำ | พร้อม | single-instance lock และ plan/ticket reconciliation |
| State และการเปลี่ยนบัญชี | พร้อมหลัง restart | state ผูกกับ login/server, บล็อกเมื่อใช้ state ผิดบัญชี และบันทึกแบบ atomic |
| Risk guardrails | พร้อม | 0.40%/setup, open cap 0.80%, internal daily stop 1.50%, FTMO daily/max 5%/10% |
| Exit สำหรับ $50K | พร้อมแบบมีข้อจำกัด | BE33 สาม legs; การเลื่อน BE เป็น client-side polling ทุก 180 วินาที |
| ข่าวและเวลาตลาด | พร้อมแบบ fail-closed | cache stale/feed ล้มเหลวแล้วไม่เปิดไม้ใหม่; holiday schedule ยังต้องอัปเดตเอง |
| Environment | ต้องทำก่อน production | สร้าง `.venv` และติดตั้งจาก `requirements-live.txt` |
| Forward evidence | ยังไม่พอ | live journal มี closed trades 0; ยังไม่มี 50 FTMO-demo trades ยืนยัน edge/cost |
| Process supervision | ยังไม่มี | ไม่มี Windows Service/watchdog หรือแจ้งเตือนนอก terminal เมื่อ process ตาย |
| Version control | ยังไม่จบ | การแก้ล่าสุดยังไม่ได้ commit/push เป็น production release |

คำตัดสินรวมคือ **CONDITIONAL GO**: execution layer พร้อมทดสอบ production แต่ยังไม่ควรตีความว่า
strategy ผ่านการยืนยัน live แล้ว จนกว่าจะมี FTMO-demo อย่างน้อย 50 closed trades

## งานบังคับก่อนเปิดบัญชี $50K

| ลำดับ | งาน | เหตุผล | เกณฑ์ผ่าน |
|---:|---|---|---|
| 1 | หยุด process เก่า PID 33196 แล้วเปิดโค้ดใหม่ | process เดิมไม่มี account binding/single-instance รุ่นล่าสุด | เหลือ `bot.main` เพียง process เดียว |
| 2 | Archive `state.json` และ `journal.jsonl` ของบัญชี $10K | ห้ามนำ initial balance, trading days และ tickets เดิมไปใช้ $50K | startup แสดง initial balance $50,000 และ login ใหม่ |
| 3 | สร้าง Python 3.13 `.venv` | environment ระดับเครื่องมี package อื่นปะปน | import MetaTrader5/pandas/numpy และ tests ผ่านใน venv |
| 4 | เลือก FTMO 2-Step Swing | กลยุทธ์อาจถือข้ามคืน/สุดสัปดาห์ | account type ยืนยันเป็น Swing |
| 5 | ตั้ง phase ให้ถูก | Challenge target 10%; Verification target 5% | terminal แสดง target ตรงกับ phase |
| 6 | ปิด Sleep/Auto restart และเปิด Algo Trading | BE33 และ timeout ต้องใช้ process ฝั่ง client | terminal heartbeat เดินต่อเนื่อง |
| 7 | ตรวจ status ก่อน LIVE | ยืนยัน symbol, lot, SL/TP, news, clock และ risk | ไม่มี `ALERT`, `MISSING_SL/TP`, `UNTRACKED` |
| 8 | Commit/tag/push release | ให้ rollback และตรวจ version ที่กำลังรันได้ | worktree สะอาดและมี production tag |

สร้าง environment:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-live.txt
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
```

## เทคนิคการเทรดที่บอทใช้

ชื่อระบบ: **HTF Quantum Adaptive**

| ขั้นตอน | หลักการ |
|---|---|
| ตลาด/TF | XAUUSD บน M15 และ M30; ใช้เฉพาะแท่งที่ปิดแล้ว |
| โครงสร้าง | Fractal 11 bars หา swing แล้วรอ BOS/CHoCH ที่ยืนยันด้วยราคาปิด |
| อายุ setup | โครงสร้างใช้ได้ 8 bars; หลังจบแผนมี cooldown 2 bars |
| Quality filter | ต้องได้อย่างน้อย 6/9 จาก HTF EMA, Local EMA, candle body, volume, ADX/DI, VWAP, CVD และ RSI |
| Trend gate | Long ต้องสอดคล้อง HTF/local trend; Short ใช้เงื่อนไขกลับด้าน |
| Entry ทันที | เข้า close เมื่อระยะจาก break ไม่เกิน 0.75 ATR |
| Entry รอย่อ | วาง limit ที่ 50% retracement; ยกเลิกเมื่อมีโครงสร้างตรงข้ามหรือเกิน 16 bars |
| Stop loss | หลัง structure ± ATR buffer 0.20; บังคับระยะให้อยู่ 0.8–2.5 ATR |
| Targets | TP1 = +1R, TP2 = +1.5R, TP3 = +2R |
| Exit ต่ำกว่า $30K | `fixed_tp3`: position เดียว ปิดทั้งหมดที่ TP3 |
| Exit ตั้งแต่ $30K | `be_33_33_34`: 33%/33%/34%; เมื่อ TP1 ปิดจริง เลื่อน TP2/TP3 ไป entry |
| Timeout | ปิดแผนที่ยัง active หลัง 120 bars |
| Invalidation | ยกเลิก/จบแผนเมื่อ opposite BOS/CHoCH ที่ผ่าน quality filter ยืนยัน |
| Position conflict | ไม่ถือ BUY และ SELL ทองพร้อมกัน; setup ใหม่ต้องผ่าน open-risk และ margin อีกครั้ง |
| News/closure | ไม่เปิดในช่วงข่าว high-impact USD -5/+3 นาที และก่อน closure ตาม schedule |

Risk ใช้เปอร์เซ็นต์จาก **initial balance** ไม่เพิ่มตาม equity:

| รายการ $50K | Percent | Cash โดยประมาณ |
|---|---:|---:|
| Risk ต่อ setup | 0.40% | $200 |
| Open risk สูงสุด | 0.80% | $400 |
| Internal daily stop | 1.50% | $750 |
| FTMO Maximum Daily Loss | 5.00% | $2,500 |
| FTMO Maximum Loss | 10.00% | $5,000 |

## ผล Backtest ของ Exit ที่ $50K ใช้

ข้อมูล 50,000 bars, chronological train/validation/locked holdout, purge 141 bars:

| Stream | Split | Trades | Win rate | Expectancy | PF | Max DD ที่ risk 0.40% |
|---|---|---:|---:|---:|---:|---:|
| M15 BE33 | Train | 698 | 46.99% | +0.1446R | 1.377 | 4.54% |
| M15 BE33 | Validation | 207 | 56.04% | +0.3778R | 2.297 | 1.86% |
| M15 BE33 | Holdout | 221 | 46.61% | +0.1888R | 1.573 | 2.24% |
| M30 BE33 | Train | 649 | 47.15% | +0.0562R | 1.126 | 9.63% |
| M30 BE33 | Validation | 200 | 52.00% | +0.2362R | 1.599 | 6.00% |
| M30 BE33 | Holdout | 204 | 56.37% | +0.3142R | 1.917 | 1.97% |

หมายเหตุ: ตัวเลือกอัตโนมัติของ research report เลือก `fixed_tp3` จาก validation ขณะที่ capital
tier $50K ใช้ BE33 ตามการตัดสินใจของ operator จึงต้องเรียก simulation ด้วย technique ให้ตรง:

```powershell
python scripts\ftmo_portfolio_sim.py --book "XAU M15 + M30" --risk 0.40 `
  --technique be_after_tp1_33_33_34 --nsim 20000
```

## โอกาสสอบผ่าน

FTMO 2-Step ปัจจุบันกำหนด target 10% แล้ว 5%, Maximum Daily Loss 5%, Maximum Loss
แบบ static 10%, อย่างน้อย 4 trading days ต่อ phase และไม่จำกัดเวลา

### ผลแบบจำลอง 20,000 paths

| สมมติฐาน edge | Step 1 | Breach ก่อน Step 1 | ผ่านครบ 2-Step | ระยะเวลารวม median / P90 |
|---|---:|---:|---:|---:|
| Holdout/current (+0.229R เฉลี่ยสอง stream) | 100.0% | 0.0% | 100.0% | 59 / 82 market days |
| Validation/base (+0.332R) | 100.0% | 0.0% | 100.0% | 42 / 55 market days |
| Train/older-flat (+0.116R) | 100.0%* | 0.0%* | 100.0%* | 112 / 178 market days |
| Stress เหลือ +0.05R/trade | 93.04% | 1.49% | 90.95% | 227 / 401 market days |
| Stress เหลือ 0.00R/trade | 32.58% | 34.03% | 19.74% | 313 / 509 market days |
| Stress เหลือ -0.05R/trade | 1.39% | 93.29% | 0.16% | 211 / 372 เฉพาะ path ที่ผ่าน |

\* 100.0% เป็นผลที่ปัดเศษจาก simulation ภายใต้ distribution ที่กำหนด ไม่ได้แปลว่า
การสอบจริงไม่มีทางล้ม

### ตัวเลขที่ควรใช้ตัดสินใจ

| คำถาม | คำตอบ |
|---|---|
| โอกาสตาม backtest ถ้า edge ยังอยู่ | ประมาณ 91–100% สำหรับครบสองขั้น |
| โอกาสถ้า live edge หายเหลือศูนย์ | ประมาณ 20% |
| ประมาณการใช้งานก่อนมี 50 demo trades | **50–70% สำหรับครบสองขั้น** |
| การันตีหรือไม่ | ไม่การันตี |

ช่วง 50–70% เป็น **operational estimate** ไม่ใช่ผลทางสถิติจากข้อมูล live เป็นการลดค่าจาก
backtest เพื่อเผื่อ regime change, FTMO/Exness feed difference, spread, commission, swap,
slippage, news filter, floating intraday DD และ BE polling delay เมื่อมี 50 FTMO-demo trades:

- expectancy หลังต้นทุน ≥ +0.05R, ไม่มี rule breach และ execution ตรงแผน: ใช้กรณี ~91%
  เป็น planning case ได้
- expectancy ระหว่าง 0 ถึง +0.05R: โอกาสจริงอยู่ในช่วงกว้างประมาณ 20–91%
- expectancy ≤ 0R: ยังไม่ควรซื้อ/รัน Challenge

## ข้อจำกัดที่ยังเหลือ

| ระดับ | ข้อจำกัด | ผลกระทบ/วิธีรับมือ |
|---|---|---|
| สูง | Closed live-demo trades ยังเป็น 0 | forward test 50 trades ก่อนเชื่อ simulation |
| สูง | Floating intraday DD ไม่อยู่ใน Monte Carlo | ใช้ risk 0.40%, open cap 0.80% และ internal stop 1.50% |
| กลาง | BE หลัง TP1 เป็น client-side 180 วินาที | เปิด MT5/bot ตลอด; SL/TP เดิมยังอยู่ broker-side |
| กลาง | ไม่มี watchdog/external alert | ใช้ Windows Task Scheduler/Service และ alert เพิ่มก่อน unattended run |
| กลาง | Holiday closures กำหนดด้วยมือ | ตรวจ FTMO notice ก่อน holiday ทุกครั้ง |
| กลาง | Verification target ต้องเปลี่ยนเป็น 5% เอง | checklist ตอนรับ login phase 2 |
| ต่ำ | News filter ทำให้จำนวนไม้ต่ำกว่า backtest | ยอมรับเพื่อ execution safety; วัดจาก journal จริง |

## แหล่งอ้างอิงกติกา

- [FTMO 2-Step Challenge](https://ftmo.com/en/2-step-challenge/)
- [FTMO Trading Objectives](https://ftmo.com/en/trading-objectives/)
- [FTMO Comparison Table](https://ftmo.com/en/comparison-table/)
