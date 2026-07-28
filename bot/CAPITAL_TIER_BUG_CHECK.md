# Quantum Desk — Capital Tier และบันทึกตรวจบัค

อัปเดตล่าสุด: 28 กรกฎาคม 2026  
ขอบเขต: `XAUUSD`, `M15 + M30`, risk 0.40% ต่อ setup, open risk รวม 0.80%

## กติกาที่ใช้งาน

บอทเลือก exit policy จาก `state.initial_balance` ซึ่งถูกบันทึกตอนเริ่มบัญชีครั้งแรก
ไม่ใช้ balance หรือ equity ปัจจุบัน จึงไม่เปลี่ยนระบบกลางทางเมื่อยอดเงินขึ้นลงผ่านเกณฑ์

| Initial balance | Exit policy |
|---:|---|
| ต่ำกว่า $30,000 | `fixed_tp3`: position เดียว ปิดทั้งหมดที่ TP3 (+2R), ไม่เลื่อน SL |
| ตั้งแต่ $30,000 | `be_33_33_34`: 33% ที่ TP1 (+1R), 33% ที่ TP2 (+1.5R), 34% ที่ TP3 (+2R) |

สำหรับ `be_33_33_34`:

1. ทั้งสาม position เปิดด้วย SL เดียวกัน
2. TP1 ต้องหายจากรายการ position ก่อน
3. บอทเลื่อน SL ของ TP2 และ TP3 ไปที่ราคา entry
4. ถ้า lot รวมแบ่งเป็นขั้นต่ำ 0.01 lot ครบสามส่วนไม่ได้ บอทปฏิเสธ setup ทั้งหมด
5. บอทจะไม่ลดเหลือ Fixed TP3 เฉพาะ setup เพราะจะทำให้ live ใช้คนละ exit policy

## การดูแล Break-even

SL/TP เริ่มต้นถูกฝากไว้ที่โบรกเกอร์ แต่เงื่อนไข “TP1 ปิดแล้วเลื่อน SL ของอีกสอง position”
ต้องอาศัยโปรแกรมที่กำลังรันอยู่ MT5 ไม่สามารถผูกเงื่อนไขนี้กับสาม position ทางฝั่ง server
ผ่าน Python API ได้โดยตรง

บอทจึงตรวจไม้แบบสาม TP ทุก 180 วินาทีระหว่างรอสแกนแท่งถัดไป การตรวจรอบนี้:

- ไม่โหลดแท่งราคาใหม่
- ไม่สร้างสัญญาณซ้ำ
- อ่าน position เพื่อดูว่า TP1 ปิดแล้วหรือยัง
- เลื่อนเฉพาะ SL ที่ทำให้ความเสี่ยงดีขึ้น
- หยุด polling ถ้าใกล้เพดาน 90% ของ 2,000 terminal requests ต่อวัน

ข้อจำกัด: ราคาอาจแตะ TP1 และย้อนถึง SL ภายในช่วงไม่เกิน 3 นาทีก่อนบอทตรวจพบ
ดังนั้น live execution ไม่สามารถรับประกันว่าจะเหมือน backtest แบบ bar simulation ทุกครั้ง
โปรแกรมและ MT5 ต้องเปิดอยู่เพื่อให้ Break-even ทำงาน ส่วน SL/TP เดิมยังทำงานแม้โปรแกรมหยุด

## บัคที่พบและแก้แล้ว

| ID | ปัญหา | การแก้ |
|---|---|---|
| CT-001 | Capital Tier อยู่เฉพาะ `settings.local.json` ซึ่งถูก gitignore ทำให้ clone จาก GitHub กลับไป Fixed TP3 | เปลี่ยน default ใน `settings.py` เป็น `capital_tier` ที่ $30,000 |
| CT-002 | ใช้ balance ปัจจุบันอาจทำให้ exit policy สลับเมื่อยอดผ่าน $30,000 | ยึด `state.initial_balance` ทั้งการเลือก policy และ sizing |
| CT-003 | State รุ่นเก่าไม่มีชื่อ exit policy ต่อ trade | infer จาก legs: หนึ่ง leg = Fixed TP3, สาม legs = BE33 แล้วบันทึกกลับได้ |
| CT-004 | Setup แบบสาม TP ถูกนับเป็นสาม concurrent trades | นับ managed setup เป็นหนึ่ง แต่ untracked ticket ยังนับแยกเพื่อความปลอดภัย |
| CT-005 | Status อาจแสดง lot หนึ่ง leg ใน split tier ทั้งที่ runtime จะปฏิเสธ | Status แสดง `REFUSED (needs 3 legs)` ให้ตรงกับ execution |
| CT-006 | Legacy `auto` อาจบันทึก tag `be33` แม้ส่งจริงเพียงหนึ่ง leg | resolve และบันทึก policy ที่ส่งจริงเป็น `fixed_tp3` หรือ `be_33_33_34` |
| CT-007 | Break-even เดิมตรวจเฉพาะตอนแท่งปิด ช้าได้ 15–30 นาที | เพิ่ม split management polling ทุก 180 วินาที |
| CT-008 | Order comment เคยติด `be33` ทั้งที่ live ใช้ Fixed TP3 | สร้าง tag `fixedtp3`/`be33` หลัง resolve policy จริง |
| CT-009 | รายการ currencies จาก JSON อาจคงเป็น list แทน tuple | แปลง `news_currencies` ตอนโหลด settings |
| CT-010 | State ใหม่ที่ `day_key` และ `paused_until_day` ว่างทั้งคู่ถูกมองว่า paused | ต้องมี server day จริงก่อน `is_paused_today` จะเป็นจริง |
| CT-011 | การปัด SL ตาม digits ของโบรกเกอร์ถูกแจ้งว่า SL กว้างกว่าระบบ | เพิ่ม price tolerance สำหรับ broker rounding |
| CT-012 | หลัง MT5/เน็ตหลุด loop เดิมเพียงรอ แต่ไม่ reinitialize session | เพิ่ม reconnect แบบ 10/20/40/60 วินาทีและ reconcile หลังกลับมา |
| CT-013 | Sizing status แสดง shortfall เป็น `0.31%!76%` ซึ่งอ่านไม่ชัด | เปลี่ยนเป็น `0.31% (76% sized)` |
| CT-014 | เปิด `main.py`/`main.bat` ซ้ำได้ ทำให้สอง process อาจส่งคำสั่งจาก signal เดียวกัน | เพิ่ม cross-process LIVE lock และปฏิเสธ instance ที่สอง |
| CT-015 | State ไม่ผูกกับ MT5 login ทำให้บัญชี $50K อาจรับ initial balance/tickets จากบัญชี $10K | ผูก state กับ login/server และ fail closed เมื่อไม่ตรง |
| CT-016 | เครื่องดับระหว่างเขียน `state.json` อาจทำให้ JSON ขาด | เขียนไฟล์ชั่วคราว, flush/fsync แล้ว atomic replace |
| CT-017 | Settings รับค่าความเสี่ยง/reconnect ที่ขัดกันได้ | เพิ่ม validation และหยุด startup เมื่อค่าหลักไม่ปลอดภัย |
| CT-018 | Simulation เลือก Fixed TP3 อัตโนมัติแม้บอท $50K ใช้ BE33 และ `--by-year` มีชื่อตัวแปรผิด | เพิ่ม `--technique`, `--book`, `--risk`, `--nsim` และแก้ report |

## จุดที่ตรวจยืนยันแล้ว

- จุดแบ่ง tier: `$29,999.99` ใช้ Fixed TP3 และ `$30,000.00` ใช้สาม TP
- ยอดปัจจุบันข้าม $30,000 ไม่ทำให้บัญชีที่เริ่มต่ำกว่าเกณฑ์เปลี่ยน policy
- Split tier ที่ SL กว้างจนแบ่งสาม legs ไม่ได้จะไม่ส่ง order
- TP1 ยังอยู่: ไม่เลื่อน SL
- TP1 หายและเหลือ TP2/TP3: เลื่อน SL ทั้งสองไป entry
- Fixed TP3 หนึ่ง position: ไม่เลื่อน SL
- สาม broker tickets จาก setup เดียวถูกนับเป็นหนึ่ง concurrent setup
- State เก่าที่ไม่มี `exit_mode` โหลดได้โดยไม่ทำให้โปรแกรมหยุด
- Pending expiration ส่งเป็น Unix timestamp integer ตามที่ MetaTrader5 ต้องการ
- News Calendar ใช้ไม่ได้หรือ stale: ไม่เปิด setup ใหม่ แต่ยังดูแล position เดิม
- Syntax/import และ JSON settings ผ่าน
- Unit tests ผ่าน 127 รายการ

คำสั่งตรวจ:

```powershell
python -B -m unittest discover -s tests -q
python -B -m compileall -q bot
python -B -m bot.code.run --status
```

`ruff` และ `pyright` ยังไม่ได้ติดตั้งในเครื่อง จึงไม่ได้ใช้เป็นหลักฐานในรอบนี้

Dependency หลักของโปรเจกต์ import ได้:

| Package | Version |
|---|---:|
| MetaTrader5 | 5.0.5735 |
| pandas | 2.3.3 |
| numpy | 2.3.5 |
| Flask | 3.1.3 |
| plotly | 6.5.0 |
| matplotlib | 3.10.8 |

`pip check` พบ version conflict ใน package ระดับเครื่องที่ไม่ได้อยู่ใน `requirements.txt`
ของบอท ได้แก่กลุ่ม Azure, LangChain, Google AI, OpenAI และ protobuf/httpx บอท live
ไม่ได้ import package เหล่านี้ใน execution path ปัจจุบัน จึงไม่ขวาง MT5 trading แต่ควรแยก
virtual environment ของโปรเจกต์ก่อนเปิดใช้ AI advisor หรือเครื่องมืออื่นใน environment เดียวกัน

## ประเด็นที่ยังต้องติดตาม

### OPEN-001 — Break-even ไม่ใช่ server-side conditional

แม้ลดเวลาตรวจเหลือ 3 นาทีแล้ว ยังมีความเสี่ยงที่ราคาย้อนเร็วกว่า polling interval
ต้องเทียบ journal/MT5 history อย่างน้อย 50 trades ว่า TP1-to-BE execution ต่างจาก backtest เท่าใด

### OPEN-002 — Pending order เคยถูกยกเลิกก่อน expiry

Order M30 `#505503364` เคยมีสถานะ `CANCELED` และ reason `EXPERT` ก่อนเวลาหมดอายุ
โค้ด expiration แบบ datetime ที่ MT5 ไม่รับถูกแก้เป็น integer แล้ว แต่เหตุการณ์ยกเลิกก่อนเวลา
ยังไม่มีหลักฐานเพียงพอว่ามาจาก process ใด หากเกิดอีกครั้งให้เก็บ journal, order history,
เวลา server และ process list ก่อน restart

### OPEN-003 — Backtest ไม่รับประกันผลสอบ

Fixed TP3 และ BE33 เป็นคนละ exit policy ต้องอ้างอิงผลของ tier ที่ใช้งานจริง
spread, commission, swap, slippage, news blackout และ polling delay ทำให้ live ต่างจาก backtest

## Position และ Pending Health

ทุก startup status และ heartbeat แสดง:

- ticket เชื่อมกับ plan/timeframe ใด
- exit mode และ role: TP1, TP2 หรือ TP3
- ราคาปัจจุบันเป็นกี่ R จาก entry
- ระยะที่เหลือถึง SL และ TP เป็น R
- SL/TP จริงที่อยู่ใน MT5
- สถานะ `PROTECTED`, `SL_AT_BE`, `MISSING_SL`, `MISSING_TP`,
  `SL_WIDER_THAN_PLAN` หรือ `UNTRACKED`
- pending entry, SL, TP และเวลาที่เหลือก่อน expiry

`[ENTRY_CAPACITY]` ตอบสถานะ `YES`, `NO` หรือ `CONDITIONAL` จาก
slot/risk/request/news gate พร้อมบอกด้านที่อนุญาตเมื่อมี position อยู่ เช่น `SELL only`
สถานะ `CONDITIONAL` หมายถึง risk room ต่ำกว่า risk มาตรฐาน แต่ล็อตที่ปัดตามขั้นต่ำของ
โบรกเกอร์อาจยังพอดี การเปิดจริงจึงต้องคำนวณ risk ของ setup นั้น รวมทั้งตรวจ signal,
margin, slippage และ guardrails อีกครั้ง

## เมื่อเน็ตหรือ MT5 หลุด

สิ่งที่ยังทำงานฝั่งโบรกเกอร์:

- SL และ TP ที่ส่งสำเร็จแล้ว
- Pending order และ expiry ที่โบรกเกอร์รับแล้ว

สิ่งที่หยุดจนกว่า connection กลับมา:

- ตรวจ TP1 แล้วเลื่อน BE
- timeout 120 bars
- reconcile journal/closed trades
- เปิด setup ใหม่

ลำดับ recovery:

1. แสดง `[CONNECTION_LOST]` และยืนยันว่า broker-side SL/TP ยังอยู่
2. รอแบบ bounded exponential backoff: 10, 20, 40 และสูงสุด 60 วินาที
3. shutdown MT5 Python session เก่าและ initialize ใหม่
4. แสดง `[CONNECTION_RESTORED]`
5. อ่าน position/pending จาก MT5 แล้ว reconcile กับ state
6. ไม่ resend ticket เดิมและไม่สร้าง duplicate order จากการ reconnect

หากคอมพิวเตอร์หรือ MT5 ปิดอยู่ SL/TP เดิมยังอยู่ที่โบรกเกอร์ แต่ Break-even เป็น client-side
จึงไม่ทำงานจนกว่าโปรแกรมกลับมา หาก position ขาด SL ให้แก้ที่ MT5 ทันที ไม่ควรรอ reconnect

## สถานะบัญชีขณะตรวจ

Snapshot เวลา server 28 กรกฎาคม 2026 12:45:

```text
Initial balance  10,000.00
Active policy    capital_tier -> fixed_tp3
Position         #505785091 SELL 0.03 @ 4033.58
SL               4047.94
TP               4003.82
Open risk        0.43%
```

Position นี้เปิดด้วย Fixed TP3 ก่อนโหลดโค้ด Capital Tier รุ่นใหม่ จึงต้องรักษา SL/TP เดิม
จนปิด ไม่แบ่งย้อนหลังและไม่เลื่อน Break-even

## วิธีเริ่มใช้โค้ดใหม่

หากมี position ค้างอยู่ สามารถปล่อย process เดิมดูแลต่อได้ เพราะบัญชี $10,000 อยู่ใน Fixed TP3
เหมือนเดิม หลัง position ปิด:

1. กด `Ctrl+C` ที่ terminal เดิม
2. ตรวจว่า MT5 ยัง login ถูกบัญชีและเปิด Algorithmic Trading
3. รัน `python bot\main.py`
4. ตรวจบรรทัด `Exit` ว่าแสดง threshold และ active policy ถูกต้อง
5. ตรวจ `Sizing`, `Open risk`, `News` และ `EXPOSURE` ก่อนปล่อยทำงาน
