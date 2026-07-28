# bot/ — ตัวรันเทรดจริงบน MT5

ชั้นรับคำสั่งเทรดของแผนใน [../docs/FTMO_SYMBOL_AND_TIMELINE.md](../docs/FTMO_SYMBOL_AND_TIMELINE.md)
คือ **XAUUSD M15 + M30, risk 0.40%/ไม้, exit BE + 33/33/34**

**กลยุทธ์ไม่ได้เขียนใหม่ที่นี่** — `signals.py` เรียก `xau.quantum.analyse` ตัวเดียวกับที่
technique lab วัดผล ทำให้สัญญาณสดกับ backtest ไม่มีทางเพี้ยนออกจากกัน โฟลเดอร์นี้เพิ่มเฉพาะ
สิ่งที่การเทรดจริงต้องมี: session MT5, การคำนวณ lot, guardrails ของ FTMO, state ที่ไม่หายเมื่อ
restart และ journal

> **ค่าเริ่มต้นคือ dry-run** ไม่มีคำสั่งไหนถูกส่งเข้าโบรกเกอร์จนกว่าจะใส่ `--live`

## เริ่มใช้

```powershell
python -m bot.code.run --status          # ดูบัญชี, guard, สถิติ R ที่ทำได้จริง
python -m bot.code.run --once            # เดินหนึ่งรอบ (dry-run) ดูว่ามันจะทำอะไร
python -m bot.code.run                   # ลูป dry-run
python -m bot.code.run --live            # ลูปจริง ส่งออเดอร์
python -m bot.code.run --flatten --live  # ฉุกเฉิน: ยกเลิกและปิดทุกอย่าง
```

**หยุดฉุกเฉินแบบไม่ต้องปิดโปรแกรม:** สร้างไฟล์เปล่าชื่อ `bot/code/STOP` → บอทหยุดเปิดไม้ใหม่ทันที
แต่ยังดูแลไม้ที่ค้างอยู่ต่อ (เลื่อน BE, ปิดตาม timeout) ลบไฟล์แล้วกลับมาเทรดต่อ

ต้องเปิด MetaTrader 5 และ login ไว้ก่อน และเปิด
Tools → Options → Expert Advisors → *Allow algorithmic trading*

## โครงสร้าง

| ไฟล์ | หน้าที่ |
|---|---|
| `settings.py` | ค่าทั้งหมดที่ปรับได้ ทับด้วย `settings.local.json` หรือ env `BOT_*` |
| `broker.py` | session MT5 ค้างไว้ตลอด process, สเปกสัญลักษณ์, อ่านราคา/พอร์ต, ส่งออเดอร์ |
| `sizing.py` | แปลง risk % → lot จากระยะ SL และแบ่ง leg แบบ largest-remainder |
| `guardrails.py` | ด่านตรวจ FTMO + ด่านของเราเอง ตัดสินว่าเปิดไม้ได้หรือไม่ |
| `signals.py` | สะพานไปยัง `xau.quantum` แปลง plan เป็น intent (`market`/`limit`/`cancel`/`wait`) |
| `trader.py` | เปิดไม้ 3 leg, เลื่อน BE หลัง TP1, ปิดตาม timeout, ปิดบัญชีผลเป็น R |
| `state.py` | `state.json` — anchor รายวัน, loss streak, ไม้ที่ถืออยู่ |
| `journal.py` | `journal.jsonl` — ทุกเหตุการณ์ + สรุปสถิติ R เทียบกับ backtest |
| `run.py` | ลูปหลัก ทำงานเมื่อแท่งปิด |

## กติกาที่บอทบังคับ

### ด่านของ FTMO (2-Step)

| ด่าน | ค่า | เมื่อชน |
|---|---:|---|
| Max loss | 10% **static** จาก balance เริ่มต้น | **หยุดถาวร** ปิดทุกไม้ และ restart ก็ไม่ล้างสถานะ |
| Max daily loss | 5% จาก balance ต้นวัน | หยุดถึงสิ้นวันของ server |
| Profit target | 10% → 5% (Verification) | รายงานความคืบหน้าใน `--status` |

| Minimum trading days | 4 วัน | นับใน `state.trading_days` และรายงานใน `--status` |

**Max daily loss คิดจาก equity ไม่ใช่ balance** (รวมกำไรขาดทุนลอย) และตัดวันที่ 00:00 CE(S)T
ซึ่งตรงกับนาฬิกา server — บอทจึงใช้ **เวลา server ของโบรกเกอร์** ไม่ใช่เวลาเครื่อง

### ด่านตามข้อห้ามของ FTMO (Forbidden Trading Practices)

| ด่าน | ค่า | ข้อห้ามที่รองรับ |
|---|---:|---|
| ห้ามถือไม้สวนทางบนทอง | เปิดใช้ | hedging สัญลักษณ์เดียวกันเป็นสิ่งห้าม → ทิ้งสัญญาณที่สวนไม้ที่เปิดอยู่ |
| Risk per trade idea | 0.80% | FTMO ตรวจ "higher Risk per Trade Idea" — คุม exposure ทางเดียวกันแยกจาก cap รวม |
| กันช่วงก่อนตลาดปิด | **3 ชม.** | gap trading: กฎห้าม 2 ชม. ก่อนตลาดปิด ≥2 ชม. เราเผื่อเป็น 3 ชม. เพราะทองบางลงก่อนปิด และไม้ที่เข้า 21:00 ศุกร์ไปไม่ถึง TP1 ก่อน gap · `bot/code/market_hours.py` หา "การปิดครั้งถัดไป" จาก **weekly close + วันหยุดใน `market_closures`** ไม่ใช่รู้จักแค่วันศุกร์ |
| ปิดยาวสั้นกว่า 2 ชม. | ไม่บล็อก | ไม่เข้าข่าย gap trading — บล็อกไปก็เสียไม้ที่กฎอนุญาต |
| News blackout | ±2 นาที | ดึงปฏิทินอัตโนมัติ กรอง high impact + USD แปลงเป็นเวลา server → [../docs/NEWS_GUARD.md](../docs/NEWS_GUARD.md) |
| นอนรอถึงแท่งปิด | ~800 calls/วัน | ลิมิต EA 2,000 server requests/วัน — ห้าม poll ถี่ |

### ด่านของเราเอง (แคบกว่า FTMO)

| ด่าน | ค่า | เหตุผล |
|---|---:|---|
| Internal daily stop | −1.50% | เผื่อระยะไม่ให้ไหลไปชน 5% ของจริง |
| แพ้ติดกัน | 3 ไม้ | หยุดวันนั้น กัน tilt และช่วง regime พัง |
| Open risk รวม | 0.80% | M15 กับ M30 มักให้สัญญาณทางเดียวกัน |
| ไม้พร้อมกัน | 2 | นับ pending order ด้วย |
| Slippage ตอนเข้า | 0.15R | ถ้าราคาวิ่งไปไกลกว่าจุดที่ backtest fill เกินนี้ → ข้ามไม้นั้น |

**ห้ามโดยดีไซน์:** ขยาย SL (`broker.move_stop` โยน error ถ้าจะขยาย), martingale, ถัวเฉลี่ยขาลง,
เพิ่ม lot หลังแพ้ — ไม่มีโค้ดรองรับสิ่งเหล่านี้เลย

รายละเอียดกฎทุกข้อและที่มา: [../docs/FTMO_RULES.md](../docs/FTMO_RULES.md)

## การเข้า-ออกไม้ ตรงกับ backtest อย่างไร

| backtest ทำ | บอททำ |
|---|---|
| เข้าที่ราคาปิดแท่งสัญญาณ (ถ้า break ≤0.75 ATR) | market order หลังแท่งปิด `entry_grace_seconds` วินาที |
| เข้าเมื่อราคาย่อ 50% ภายใน 16 แท่ง | limit order ที่ราคานั้น พร้อม expiry = 16 แท่ง |
| SL = structure ± 0.2 ATR จำกัด 0.8–2.5 ATR | ใส่ SL ติดไปกับออเดอร์ทุก leg |
| TP1/TP2/TP3 = 1R/1.5R/2R น้ำหนัก 33/33/34 | 3 position แยกกัน แต่ละตัวมี TP ของตัวเอง |
| TP1 ติดแล้วเลื่อน SL ที่เหลือมาที่ entry | ตรวจว่า leg TP1 หายไปแต่ leg อื่นยังอยู่ → เลื่อน SL |
| ปิดถ้าถือเกิน 120 แท่ง | ปิด market ตาม `MAX_TRADE_BARS` เดียวกัน |
| ไม่ resolve TP/SL บนแท่งที่ fill | บอทเห็นเฉพาะแท่งที่ปิดแล้ว (`Broker.bars` ตัดแท่งกำลังวิ่งออก) |

เหตุผลที่แยกเป็น 3 position ไม่ใช่ปิดบางส่วน: TP/SL ฝากไว้ที่โบรกเกอร์ทั้งหมด **ถ้าโปรแกรมนี้ตาย
หรือเน็ตหลุด ทุกไม้ยังมีทางออกของตัวเอง**

`history_bars = 3000` ตรึงไว้เป็นค่าคงที่ เพราะ state machine ของ Pine ขึ้นกับลำดับข้อมูล
ถ้าเปลี่ยนจำนวนแท่งไปเรื่อย ๆ plan จะไม่ reproducible

## ขนาดบัญชีที่ต้องมี

lot = risk_cash ÷ (ระยะ SL × value/point) แล้วปัดลงตาม lot step
บน XAUUSD 1 lot = 100 oz → 1 จุดราคา = \$100

ที่ risk 0.40% และระยะ SL ตามค่ากลางของ study (M15 ≈ \$11.05, M30 ≈ \$16.16):

| Balance | M15 | M30 |
|---|---|---|
| \$10,000 | 0.03 = 0.01/0.01/0.01 | 0.02 → **เหลือ 1 leg** (ออกที่ TP2) |
| \$12,500 | 0.04 = 0.01/0.01/0.02 | 0.03 = 0.01/0.01/0.01 |
| \$25,000 | 0.09 = 0.03/0.03/0.03 | 0.06 = 0.02/0.02/0.02 |
| \$50,000 | 0.18 = 0.06/0.06/0.06 | 0.12 = 0.04/0.04/0.04 |
| \$100,000 | 0.36 = 0.12/0.12/0.12 | 0.24 = 0.08/0.08/0.08 |

เกณฑ์ขั้นต่ำที่จะแบ่ง 3 leg ได้ = **750 × ระยะ SL เป็นดอลลาร์** → M15 ต้องมี ~\$8,300
M30 ~\$12,100 และ H1 ~\$17,600

**ผลกระทบ:** บัญชี \$10,000 จะรัน M30 ได้แค่ leg เดียว ซึ่งไม่ใช่ exit ที่ backtest วัด
ถ้าใช้บัญชี \$10k ให้เทรด **M15 เท่านั้น** หรือขยับไปบัญชี \$25k ขึ้นไป
บอทจะไม่ปัด lot ขึ้นเพื่อให้พอ — ถ้าเล็กกว่าขั้นต่ำของโบรกเกอร์มันจะปฏิเสธไม้นั้นและบันทึกเหตุผล

## ตรวจว่าตรงกับ backtest หรือไม่

`journal.summarise()` คืนค่าในรูปเดียวกับรายงาน technique lab:

```powershell
python -c "from bot.code import journal; from bot.code.settings import JOURNAL_PATH; print(journal.summarise(JOURNAL_PATH))"
```

เทียบ `expectancy_r` กับ holdout ของ TF นั้นใน `outputs/backtests/technique_lab/XAUUSD/<TF>/report.json`
ถ้าห่างกันเกิน ~0.05R ต่อไม้หลังผ่านไป 50+ ไม้ แปลว่า slippage/สเปรดจริงกินมากกว่าที่ประเมิน
→ กลับไปดู `--by-year` และตาราง cost sensitivity ก่อนเทรดต่อ

## Checklist ก่อนกด --live

1. `python -m unittest discover -s tests -q` ผ่านหมด
2. `python -m bot.code.run --once` แล้วอ่าน log ว่าจะส่งอะไร lot เท่าไร SL/TP ที่ไหน
3. รัน dry-run ทิ้งไว้อย่างน้อย 1 สัปดาห์ เทียบ intent ที่มันเห็นกับกราฟด้วยตา
4. ตั้ง `initial_balance` ให้ตรงกับ balance เริ่มต้นของบัญชี challenge
5. ยืนยันกฎล่าสุดที่ [FTMO Trading Objectives](https://ftmo.com/en/trading-objectives/)
   — ตัวเลขใน `settings.py` เป็นค่าที่บันทึกไว้ ณ 2026-07-26 ไม่ใช่ค่าที่ดึงสด
6. เทรด demo FTMO จริง 4–6 สัปดาห์ก่อนซื้อ challenge เพื่อวัด slippage บน feed ของ FTMO
   (ข้อมูลที่ใช้ backtest มาจาก Exness ซึ่งสเปรดต่างกัน)

## ข้อจำกัดที่ยังเหลือ

- ฟีดข่าวเป็นรายสัปดาห์ ต้องดูค่า `age` ใน `--status` ว่า cache ไม่เก่าเกินไป
- news guard บล็อกแค่ **การเปิด** ไม้ ไม่ได้บล็อกการปิด เพราะ SL/TP ฝากไว้ที่โบรกเกอร์
- `weekly_close_hour` ตั้งไว้ 23:00 server ต้องเทียบกับ spec เวลาปิดทองของ FTMO เอง
- `fallback_server_utc_offset = 3.0` สำหรับ Exness → เปลี่ยนเป็น `2.0` เมื่อย้ายไป FTMO
- ถ้าผ่านไปถึง funded ต้องเลือก **Swing account** เพราะทอง M15/M30 ถือข้ามคืนและข้ามสุดสัปดาห์
  (timeout 120 แท่ง M30 = 60 ชั่วโมง) ซึ่งบัญชี Standard มีข้อจำกัดตอน funded
- ไม่มี trailing stop นอกจาก BE ครั้งเดียว (ตรงกับ backtest)
- คิด R จาก `(profit + commission + swap) / risk_cash` — `deal.profit` ของ MT5 เป็นผลจากราคาเท่านั้น
  ต้องบวกอีกสองฟิลด์เอง ไม่ทำแล้ว R ที่วัดได้จะดีกว่าเงินในบัญชีจริง ซึ่งเป็นตัวเลขที่ใช้ตัดสิน edge
  ค่า `costs` ในแต่ละ `trade_closed` คือส่วนที่หักไป ดูได้ว่าต้นทุนกินไปเท่าไร
- ยังไม่รองรับหลายสัญลักษณ์ในโปรเซสเดียว (ออกแบบไว้สำหรับทองเท่านั้นตามผล study)
- `--flatten` ปิดด้วยราคาตลาด ถ้าตลาดปิดจะโยน error ให้เห็น ไม่ปิดเงียบ
