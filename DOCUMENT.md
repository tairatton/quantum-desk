# Quantum Desk — เอกสารระบบฉบับเต็ม

ปรับปรุง 6 สิงหาคม 2026 · หลังแยกสองต้นไม้ แก้บั๊กจากการ audit และปรับตั้งค่าความเสี่ยง

สารบัญ
1. [ระบบนี้คืออะไร](#1-ระบบนี้คืออะไร) · 2. [ทำอะไรได้บ้าง](#2-ทำอะไรได้บ้าง) ·
3. [ผลลัพธ์](#3-ผลลัพธ์) · 4. [ความเสี่ยง](#4-ความเสี่ยง) ·
5. [โครงสร้าง](#5-โครงสร้างโปรแกรม) · 6. [บั๊กที่แก้](#6-บั๊กที่พบและแก้) ·
7. [เทอร์มินัล](#7-เทอร์มินัล) · 8. [สถานะการทดสอบ](#8-สถานะการทดสอบ) ·
9. [ยังไม่เสร็จ](#9-สิ่งที่ยังไม่เสร็จ) · 10. [เช็คลิสต์ตรวจสอบ](#10-เช็คลิสต์ตรวจสอบ)

---

## 1. ระบบนี้คืออะไร

บอทเทรดอัตโนมัติสำหรับบัญชีทดสอบ prop firm สองเจ้า ใช้กลยุทธ์เดียวกัน

| | forex | future |
|---|---|---|
| Prop firm | FTMO 2-Step | TopStep Combine |
| ช่องทาง | MetaTrader 5 | ProjectX Gateway (REST) |
| สินค้า | XAUUSD spot | MGCZ26 (Micro Gold 10 oz) |
| หน่วยไซซ์ | lots | contracts (จำนวนเต็ม) |
| หน่วยความเสี่ยง | % ของทุนตั้งต้น | ดอลลาร์ |
| ขนาดบัญชี | $50,000 | $50,000 |
| สถานะ | **รันจริงอยู่** | **ยังไม่ commission** |

ทองคำทั้งคู่ ต่างที่รูปสัญญา — spot 1 lot = $100/จุด · MGC 1 สัญญา = $10/จุด

### กลยุทธ์

HTF Quantum Adaptive อ่านโครงสร้างราคาบน M15 และ M30 วางแผนเข้าไม้พร้อมเป้าสามระดับ
โค้ดอยู่ที่ `strategy/quantum.py` และบอทเรียกตัวเดียวกับที่ backtest ใช้ กันไม่ให้ระบบที่รันจริง
เพี้ยนจากระบบที่วัดผลไว้

### เทคนิคออก (เหมือนกันทั้งสอง venue)

`be_after_tp1_33_33_34` — แบ่งสามขา 33/33/34

1. TP1 โดน → ขาที่เหลือย้าย SL ไป **breakeven ที่ครอบคลุมต้นทุน** (ราคา fill จริง + commission
   + slippage + swap สะสม ไม่ใช่ราคาเข้า)
2. TP2 โดน → ขาสุดท้ายย้าย SL ขึ้นไป **ล็อกที่ระดับ TP1**
3. ขาสุดท้ายวิ่งไป TP3

ไซซ์เล็กเกินจะแตกสามขา (forex: ทุน < $30,000 · future: < 3 สัญญา) ใช้ `fixed_tp3` ขาเดียว —
ไม่ใช่การประนีประนอมสองขาที่ไม่เคยถูกวัด

### Dynamic risk ladder

ไต่ลงตาม drawdown จาก high-water mark ของยอดปิดจริง กฎเดียวกัน ต่างแค่หน่วย

| ขั้น | forex | ขั้น | future |
|---|---|---|---|
| DD < 0.50% | 1.00% | DD < $250 | **$400** |
| DD < 1.00% | 0.75% | DD < $500 | $300 |
| DD < 1.50% | 0.50% | DD < $750 | $250 |
| DD ≥ 1.50% | 0.40% | DD < $1,000 | $200 |
| | | DD < $1,250 | $100 |
| | | DD ≥ $1,250 | $50 |

ฝั่งฟิวเจอร์สมีสองขั้นล่างเพิ่ม (`recovery_risk_dollars`) เพราะบัญชีที่ถอยจนเหลือห้องน้อยกว่า
$200 จะเทรดไม่ได้อีกเลย — ยังไม่ตก แต่ผ่านก็ไม่ได้

tier บนสุดถูกผูกกับ `internal_daily_stop_dollars` เสมอ: tier $500 ใต้ daily stop $400 คือขนาดที่
ไม้แรกของวันเข้าไม่ถึงตลอดกาล

### Guard ที่กันไม่ให้บัญชีตาย (ฝั่งฟิวเจอร์ส)

- `remaining_room()` — ระยะถึง floor ที่ใกล้ที่สุดในสามชั้น (max loss / daily / internal)
- `loss_room_reserve_dollars` $200 — กันไว้เหนือ **floor ที่ฆ่าบัญชีเท่านั้น** เพราะชน daily limit
  คือโดนล็อกวันเดียว ไม่ใช่สอบตก
- `fit_to_room()` — ถ้า tier ที่ต้องการไม่พอดีห้อง ให้ลดลงมาเป็น tier ที่พอดี ถ้าไม่มีเลย = ไม่เทรด

ฝั่ง FTMO มี projected daily/max-loss guard ใน `bot/forex/core/bot/guardrails.py` และเรียกก่อนเข้าไม้
แล้ว แต่ยังไม่ได้ใช้ fixed-reserve room model แบบ TopStep ใน simulator ดังนั้นผล decay ของสอง
ต้นไม้เทียบกันตรง ๆ ไม่ได้จนกว่าจะรันด้วย harness และ seed เดียวกัน

---

## 2. ทำอะไรได้บ้าง

ดับเบิลคลิก `bot/forex/main.bat` (เข้า live ทันที) หรือ `python bot/forex/main.py` (เมนู) — คำสั่ง
ย่อยรันจากใน `core/` ของต้นไม้นั้น:

```bash
cd bot/forex/core
python -m bot.run --status            # สถานะบัญชี guard และสถิติ R จริง
python -m bot.run --once              # เดินหนึ่งรอบ dry run
python -m bot.run --live              # ส่งออเดอร์จริง
python -m bot.run --flatten --live    # ฉุกเฉิน ยกเลิกและปิดทุกอย่าง
python -m entrypoints.main            # เมนูภาษาไทย
python tools/ftmo_portfolio_sim.py    # XAUUSD M15+M30 production sim
python tools/ftmo_portfolio_sim.py --book "+ BTC M5"  # explicit research comparison
python -m entrypoints.research backtest  # วัดผลทุก symbol × timeframe
python -m pytest test/unit -q
```

```bash
cd bot/future/core
python -m entrypoints.main                  # เทอร์มินัลเมนู
python -m entrypoints.main --status --offline  # สถานะหน้าเดียว ไม่ต้องต่อเน็ต
python -m entrypoints.live --check         # ทดสอบเชื่อมต่อ อ่านอย่างเดียว
python tools/topstep_sim.py                # default: MGC Yahoo + dynamic room guard
python tools/topstep_sim.py --flat         # fixed-risk reference only
python tools/download_mgc_yahoo.py --period 60d  # free MGC smoke-test bars
python tools/test_mgc_dry_run.py            # strategy + 1-contract sizing, no broker
python -m pytest test/unit -q
```

รันจากใน `core/` ของต้นไม้เท่านั้น — `python -m future.entrypoints.live` จาก root จะแจ้งเตือนและออก
เพราะแต่ละต้นไม้เป็น import root ของตัวเอง

### สิ่งที่บอทดูแลอัตโนมัติ

- เวลาตลาด วันหยุด และช่วงพักบำรุงรักษา CME (ฟิวเจอร์ส: flat 15:10 CT · เปิด 17:00 CT)
- หยุดเข้าไม้รอบข่าว USD ระดับสูง — fail closed ถ้าดึงปฏิทินไม่ได้
- นับวันเทรด นับโควตา request กันรันซ้อนสองโปรเซส
- state ทนรีสตาร์ต — high-water mark ไม่ถูกรีเซ็ต
- kill switch เป็นไฟล์: สร้าง `STOP` แล้วหยุดเข้าไม้ใหม่ ไม้เดิมยังถูกดูแลต่อ

---

## 3. ผลลัพธ์

### 3.1 ผลกลยุทธ์ดิบ (XAUUSD holdout ที่ไม่เคยใช้เลือกเทคนิค)

| TF | ไม้ | Win rate | Expectancy | PF | Max DD (R) |
|---|---|---|---|---|---|
| M15 | 248 | 40.3% | +0.176R | 1.49 | 9.07R |
| M30 | 227 | 57.7% | +0.357R | 2.07 | 6.09R |

รวม book: **+0.236R ต่อไม้ · 3.1 ไม้/วัน · +0.721R/วัน**

### 3.2 FTMO — 20,000 paths, production settings

คำสั่งหลักใช้ `python tools/ftmo_portfolio_sim.py --production`; ถ้าส่ง `--risk`
จะเป็น fixed-risk experiment ไม่ใช่ risk path ของ live bot

| Regime | ผ่าน Step 1 | breach | ผ่านสองด่าน | Step 1 | **รวม** | DD med | DD p99 |
|---|---|---|---|---|---|---|---|
| holdout | 100.0% | **0.0%** | **100.0%** | 19/37 | **31/52 วัน** | 1.59% | 4.25% |
| validation | 100.0% | 0.0% | 100.0% | 16/28 | 25/40 | 1.41% | 3.56% |
| train (แบน) | 100.0% | 0.0% | 100.0% | 35/79 | 57/111 | 2.54% | 7.08% |

`breach` รวมการตกทั้ง Step 1 และ Step 2; DD/worst day หยุดนับทันทีที่ Step 1
ผ่านหรือตก ไม่รวม generated tail หลัง evaluation จบ

**≈ 2.5 เดือน** · เคสช้า 3.5 เดือน · regime แบน 4.5–7 เดือน

### 3.3 TopStep — 20,000 paths, ladder + room guard

คำสั่งนี้ใช้เฉพาะข้อมูล Yahoo `MGC=F` ใน `bot/future/core/test/data/market/MGC/`
เท่านั้น ไม่อ่านรายงาน XAUUSD ของ Forex
(`python tools/topstep_sim.py` จะปฏิเสธ scenario ของ Forex)

| Regime | ผ่าน | ตก | ยังไม่จบ | **วัน med/p90** | DD med | DD p95 |
|---|---|---|---|---|---|---|
| MGC Yahoo 60 วัน | **100.0%** | **0.0%** | 0.0% | **13 / 28** | $454 | $960 |

เป็น smoke/short-sample result ไม่ใช่ long-run pass probability; Yahoo เป็น delayed/rolled
feed และ daily-net model ยัง optimistic ต่อ intraday loss path

### 3.4 DD ต่อวันและ Max DD

| | forex | future |
|---|---|---|
| DD/วัน บอทหยุดเอง | 1.50% ($750) | $400 (0.80%) |
| DD/วัน เพดาน firm | 5.00% ($2,500) | $1,000 (2.00%) |
| exposure รวมสูงสุด | — | $800 (< DLL) |
| Max DD median | 1.59% | $400 |
| Max DD p95–p99 | 4.25% (p99) | $1,160 (p95) |
| เพดานที่ทำให้ตก | 10.00% | $2,000 |
| ใช้ไปกี่ % ของระยะที่ตาย | ~43% | ~58% |

### 3.5 ทำไม TopStep เร็วกว่า 5 เท่า

ไม่ใช่เพราะระบบดีกว่า — กำไรต่อวันเท่ากัน (~$361) แต่เป้าเล็กกว่า

```
FTMO   : 15% (10% แล้ว 5% อีกรอบ) = ~$7,500 สองรอบ
TopStep: 6%  ($3,000 ครั้งเดียว)   -> $3,000 / $361 ≈ 8.3 วัน
```

---

## 4. ความเสี่ยง

### 4.1 edge หด — ความเสี่ยงที่วัดเป็นตัวเลขได้

| edge เหลือ | FTMO ตก | TopStep ตก |
|---|---|---|
| +0.236R (ที่วัดได้) | 0.0% | 0.0% |
| +0.150R | 0.0% | 0.0% |
| +0.100R | 0.2% | **0.0%** |
| +0.050R | 4.2% | **0.0%** |
| +0.020R | 23.3% | **0.0%** |
| 0.000R | 48.2% | **0.0%** |

TopStep ใช้ room guard + reserve ให้บอทยืนเฉยแทนที่จะเข้าไม้ที่จะฆ่าบัญชี ส่วน FTMO live มี
projected daily/max-loss guard ของตัวเอง แต่ simulator ยังเป็น daily-net approximation และยัง
ไม่ได้จำลอง fixed reserve แบบ futures ดังนั้นตัวเลข edge = 0 ในตารางนี้เป็น historical result
ที่ต้องรันซ้ำด้วย deterministic harness ก่อนใช้เป็นข้อสรุป

### 4.2 บทเรียนที่วัดได้ระหว่างปรับ

- **ลด risk ไม่ได้ทำให้ปลอดภัยขึ้น** ที่ edge +0.05R: risk $200 ตก 1.6% แต่ $75 ตก 7.4% เพราะ
  ไม้เล็กใช้เวลานานกว่า = อยู่ในความเสี่ยงนานกว่า
- **ใช้เพดาน DLL ให้หมดไม่ได้เร็วขึ้น** internal stop $400 → train ผ่าน 99.2% · $1,000 → 95.7%
  โดยเวลาเท่าเดิม เพราะขาดทุนวันใหญ่ดึง trailing floor ขึ้นมาเร็วกว่า
- **reserve ผิดที่ทำให้ช้าเป็นเท่าตัว** เอาไปกัน daily floor ด้วยทำให้เพดานต่อไม้เหลือ $200 และ
  ladder ทั้งชุดใช้ไม่ได้ (19 วัน) พอกันเฉพาะ floor ที่ฆ่าบัญชีก็กลับมา 10 วัน โดย fail ยังเป็น 0

### 4.3 ความเสี่ยงเชิงโครงสร้าง

| ความเสี่ยง | ผลถ้าเกิด | สถานะ |
|---|---|---|
| edge ของ future วัดบน MGC ได้เพียง Yahoo 60 วัน | ตัวเลข 100% ยังสรุประยะยาวไม่ได้ | ต้องเพิ่ม ProjectX/TopStep history · `--live` ถูกล็อก |
| endpoint ProjectX ยังไม่เคยยิงจริง | ออเดอร์อาจไม่ออกหรือออกผิด | `COMMISSIONED = False` |
| กฎ TopStep มาจากแหล่งรอง | บอทคิดว่าปลอดภัยทั้งที่ตายแล้ว | มีคอมเมนต์เตือนทุกจุด |
| MLL เช็ค real-time รวมกำไรลอยตัว | ซิมเห็นแค่ยอดรายวัน = มองโลกสวย | ยังไม่จำลอง |
| consistency rule | วันดีสุด median $1,120 · เพดาน $1,500 | เกินแล้วเป้าขยับ ไม่ตก |
| engine/strategy ก๊อปสองชุด | แก้บั๊กที่หนึ่งไม่ใช่แก้ที่สอง | ตั้งใจแลกเพื่อแยกขาด |
| roll สัญญาเป็น manual | MGCZ26 หมดอายุแล้วบอทไม่รู้ | broker ปฏิเสธถ้าเจอหลาย expiry |
| spread FTMO แพงกว่า backtest 2 เท่า | −0.012 ถึง −0.018R/ไม้ | คิดเข้าไปแล้ว |

### 4.4 สิ่งที่ซิมไม่ครอบคลุม

1. การปัดเป็นสัญญาเต็มใบ — ซิมคิด risk ต่อเนื่อง ของจริงกระโดดเป็นขั้นและมักต่ำกว่าที่ขอ
2. gap ข้ามคืน/สุดสัปดาห์ — SL อาจถูกข้าม (reserve $200 ช่วยได้บางส่วน)
3. feed ค้าง ไฟดับ เน็ตหลุด — มี guard แต่ไม่อยู่ในซิม
4. **daily stop ถูก clip จากยอดสุทธิรายวัน ไม่ใช่ลำดับไม้** — วันที่ −$500 แล้ว +$800 ซิมเห็นเป็น
   +$300 ทั้งที่ของจริงล็อกไปตั้งแต่ −$400 ประกาศไว้ในหัวรายงานทั้งสองซิม
5. FTMO: กฎแพ้ 3 ไม้ติดหยุดวัน ยังไม่ได้จำลอง
6. ทั้งสองบัญชีเทรดทองเหมือนกัน รันพร้อมกันคือความเสี่ยงทางเดียวกันสองเท่า ไม่ใช่การกระจาย

---

## 5. โครงสร้างโปรแกรม

```
quantum-desk/
├── README.md · DOCUMENT.md
└── bot/
    ├── forex/                  FTMO · MT5 · XAUUSD
    │   ├── main.py             เมนู (ดับเบิลคลิกไม่ได้ตรง ๆ — รัน main.bat หรือ python main.py)
    │   ├── main.bat            ดับเบิลคลิกเข้า live loop ทันที
    │   └── core/
    │       ├── entrypoints/    main.py(จริง) · live.py · research.py
    │       ├── bot/            settings · broker(MT5) · guardrails(FTMO) · run · trader · live
    │       ├── engine/         instrument · sizing · state · journal · news · market_hours · dynamic_risk
    │       ├── strategy/       quantum · technique_lab · backtest_reporting · webapp · mt5_source
    │       ├── tools/          ftmo_portfolio_sim · build_ftmo_report · plot · forward_check · launch
    │       ├── test/           unit(292) · docs · data · outputs · pine
    │       └── (runtime state stays under core/bot/ for live compatibility)
    └── future/                 TopStep · ProjectX · MGCZ26
        ├── main.py             เมนู
        ├── main.bat            ดับเบิลคลิกเปิดเมนู
        └── core/
            ├── entrypoints/    main.py(จริง) · live.py
            ├── bot/            settings · broker(ProjectX) · guardrails(TopStep) · trader · live
            ├── engine/         สำเนา + decide_dollars · ladder_steps · fit_to_room
            ├── strategy/       สำเนา (webapp ถูกปิดไว้)
            ├── tools/          topstep_sim
            └── test/           unit(107) · docs · data · outputs
```

`main.py`/`main.bat` ที่ root ของแต่ละต้นไม้เป็นจุดเดียวที่ผู้ใช้ต้องเห็น — ที่เหลือทั้งหมดถูกเก็บไว้ใต้
`core/` ให้โฟลเดอร์ไม่รก ตัว `main.py` แค่เติม `core/` เข้า `sys.path` แล้วเรียก
`core/entrypoints/main.py:main()` ของจริง ไม่มีตรรกะซ้ำ

**กฎเหล็ก:** ไม่มี import ข้ามต้นไม้ · `engine` ไม่ import `bot` แม้แต่ตอน TYPE_CHECKING (ใช้
Protocol) · env แยก `BOT_*` / `FUT_*` — การแก้ฝั่งฟิวเจอร์สจึงแตะบัญชี FTMO ที่รันเงินจริงไม่ได้

**ราคาที่จ่าย:** `engine/` และ `strategy/` มีสองชุด แก้บั๊กต้องแก้สองที่

---

## 6. บั๊กที่พบและแก้

### 6.1 จาก refactor (path)

| บั๊ก | ผลถ้าไม่แก้ |
|---|---|
| `ftmo_portfolio_sim` โหลด `bot/code/settings.py` ผ่าน importlib | ซิมพังทั้งตัว · grep หาโมดูลไม่เจอเพราะเป็น path string |
| `main.bat` cd ผิดระดับ + `.venv` path ผิด | ดับเบิลคลิกแล้วบอทไม่ขึ้น |
| เทสต์ hardcode `ROOT/"data"/"market"` | เทสต์แดงหลังย้ายโฟลเดอร์ |
| `webapp.py` ชี้ `templates/` เดิม | หน้าเว็บ 500 |
| ไม่มีไฟล์ MGC Yahoo cache | `topstep_sim` แจ้งให้ download ก่อน และไม่อ่านข้อมูล Forex |
| `__pycache__` 71 ไฟล์ + `.pytest_cache` ถูก commit | repo สกปรก |
| `bot/code/journal.jsonl` มี 142 บรรทัดที่ไม่มีในไฟล์ใหม่ (6 trade_opened · 5 trade_closed) | ประวัติเทรดจริงหาย |

### 6.2 ตรรกะในโค้ดฟิวเจอร์ส (รอบแรก)

| บั๊ก | ผลถ้าไม่แก้ |
|---|---|
| `split_contracts` ตัดเศษ — 6 สัญญาได้ (1,1,4) | 17/17/66 ที่อ้างว่าเป็น 33/33/34 = คนละระบบกับที่วัด |
| `session_open` เช็คแค่วันเสาร์ | เช้าวันอาทิตย์นับว่าตลาดเปิด ทั้งที่เปิดอีก 8 ชม. |
| บล็อกทุกอย่างหลัง 15:10 | ทิ้งเซสชันกลางคืนทั้งหมดที่ M15/M30 เทรด |
| `live.py` ส่งเวลา naive local | เครื่องกรุงเทพคลาด 12 ชั่วโมง |
| ไม่มี TP2 step | ขาสุดท้ายค้างที่ BE ไม่ล็อก TP1 |
| ไม่มี dynamic risk ladder | เสี่ยงคงที่ ไม่หดตอน DD |

### 6.3 จาก audit ภายนอก

| บั๊ก | ผลถ้าไม่แก้ |
|---|---|
| `max_loss_floor` ใช้ balance ปัจจุบันเป็น anchor | **floor เดินลงได้** — เสีย $2,000 แล้วเสียอีก $2,000 โดยไม่ breach |
| `eod_balance_high_water` ไม่มีใครเขียน | trailing floor ค้างที่ยอดตั้งต้นตลอดชีพบัญชี |
| `can_open` ไม่ดูระยะที่เหลือ | เหลือ $100 เหนือ floor แต่รับไม้ $200 = stop โดนแล้วตาย |
| contract cap เป็น 5 ทั้งที่ MGC เป็น micro (50) และเช็คทีละ setup | สอง setup × 50 = 100 สัญญา เกินกฎ |
| `progress()` ใช้เป้า $3,000 ตายตัว | กำไร $3,000 + วันดีสุด $2,000 → บอกให้ยื่นสอบทั้งที่เป้าจริงคือ $4,000 |
| `min_trading_days = 2` | สร้างเงื่อนไขที่ Combine ไม่มี |
| `open_trade` จับแค่ `OrderRejected` | ขาแรกผ่าน ขาสองเน็ตหลุด → exception หลุด ไม่มีใครรู้ว่ามีไม้ค้าง |
| นับ accepted เป็น filled | position จริงไม่ตรงกับที่ manage |
| ladder ปิดอยู่ + `fitting_tiers` อ่าน field ที่ไม่มี | ไม่เคยไต่ tier และ AttributeError ทันทีที่ถูกเรียก |
| state เป็น MT5/Prague | bind บัญชี ProjectX ไม่ได้ · วันตัดผิด 7 ชั่วโมง |
| ladder mode ไม่สน `--require-winning-days` | เงื่อนไขที่เป็นไปไม่ได้ให้ผลเท่าเดิม |
| สถิติซิมนับวันหลังจบไปแล้ว | DD/best day เพี้ยน สองโหมดไม่ตรงกัน |
| FTMO sim ไม่มี 4-day minimum และ internal stop | ผ่านตั้งแต่วันแรกได้ · นับ tail ที่บอทไม่มีวันเจอ |
| `webapp.py` ฝั่งฟิวเจอร์สไม่มี template | TemplateNotFound ทันทีที่เรียก |
| `python -m future.entrypoints.live` | ImportError ลึก ๆ ไม่บอกสาเหตุ |
| `engine` import `bot` ตอน TYPE_CHECKING | ผิดกฎ dependency ของโปรเจกต์ |

### 6.4 จากการปรับตั้งค่า

| บั๊ก | ผลถ้าไม่แก้ |
|---|---|
| reserve กัน daily floor ด้วย | เพดานต่อไม้เหลือ $200 · ladder ทั้งชุดใช้ไม่ได้ · ช้าเป็นเท่าตัว |
| ladder หยุดที่ $200 ตอน DD ลึก | บัญชีถอย $1,400 เสี่ยงเท่าบัญชีถอย $760 ตรงที่ floor ใกล้สุด |
| ไม่มี tier ต่ำกว่า $200 | บัญชีที่เหลือห้อง < $200 เทรดไม่ได้อีกเลย = จอดถาวร |
| tier บนสุด $500 ใต้ daily stop $400 | ขนาดที่ไม้แรกของวันเข้าไม่ถึงตลอดกาล |
| `max_open_risk_dollars` = $1,000 = DLL พอดี | สอง stop วันเดียวกัน = ชน DLL เป๊ะ |

ทุกข้อมีเทสต์กันถอยหลัง

---

## 7. เทอร์มินัล

`python -m entrypoints.main` ตอบสามคำถามในหน้าจอเดียว: เหลือระยะเท่าไรก่อนตก · ตลาดเปิดไหม ·
ไม้ถัดไปกี่สัญญา

```
==============================================================================
  FUTURES · TopStep · MGC · M15, M30
  Thu 2026-08-06 01:49 CDT  exchange clock
==============================================================================
[CONNECTION] not attempted     [SESSION] ok     [HEALTH] ok
[COMMISSIONED] no — --live is refused until the checklist is done

  Balance               $51,500.00        live
  Started at            $50,000.00        high water $51,500.00 EOD
  Max loss floor        $49,500.00        trailing end-of-day
  Room to the floor     $2,000.00         ............................
  Tradeable room        $1,800.00         $200 reserved, never risked
  Today's loss          $0.00 of $1,000   ............................
  Internal stop         $50,800.00        bot stands down at -$400
  Target                $+1,500.00 of $3,000   ##############..............

  Risk per trade        $400              drawdown ladder
    stop 5 pts          8 contracts       $400 real risk · 3/3/2
    stop 20 pts         2 contracts       $400 real risk · 1 leg to TP3
    stop 40 pts         1 contract        $400 real risk · 1 leg to TP3
==============================================================================
```

หลักการ: **วาดได้โดยไม่ต้องต่อเน็ต** (`--offline` อ่านจาก state ล้วน) · แถบแทนตัวเลขสำหรับเพดาน
เปลี่ยนสีที่ 50%/80% · แสดงความเสี่ยงจริงหลังปัด ไม่ใช่ที่ขอ และบอกตรง ๆ ว่า stop ไหน "no trade" ·
ปิดสีเมื่อ pipe ลง log · คำสั่งที่ส่งเงินออกต้องพิมพ์คำยืนยันตัวพิมพ์ใหญ่

---

## 8. สถานะการทดสอบ

| ต้นไม้ | เทสต์ | ผล |
|---|---|---|
| forex | 292 + 19 subtests | ผ่านทั้งหมด |
| future | 107 | ผ่านทั้งหมด |

ตรวจเพิ่ม: import ทุกโมดูลสองต้นไม้ผ่าน · path ที่ resolve จริงมีไฟล์อยู่ทุกจุด · entry point ทุกตัว
รันได้ · `bot.run --status` อ่าน state เดิมครบ · ไม่มี import ข้ามต้นไม้ · ไม่มี `engine` ที่ import
`bot`

---

## 9. สิ่งที่ยังไม่เสร็จ

1. **`bot/future/core/bot/run.py` ยังไม่มี** — มีตั้งแต่ signal → sizing → ส่งออเดอร์ และเทอร์มินัล แต่ยังไม่มี
   loop ที่ consume fill, amend bracket ของขาที่เหลือ, บังคับ flatten 15:10, save state และ
   reconcile หลัง restart · `stop_after_tp1`/`stop_after_tp2` ยังไม่มีที่เรียกใน production
2. **`eod_balance_high_water` เขียนแล้วใน `roll_day()`** แต่ต้องมี run loop มาเรียกทุกสิ้นวัน
3. **ยืดประวัติ MGC ให้ยาวขึ้น** — simulator ใช้ MGC Yahoo 60 วันแล้ว แต่ยังไม่ใช่
   ProjectX/TopStep execution feed และยังไม่มี long-history acceptance benchmark
4. **ยืนยันกฎ TopStep กับ rulebook ทางการ**
5. **ทดสอบ ProjectX endpoint กับ demo key**
6. **ทำ deterministic decay harness และทดสอบ room/reserve model แยกตาม venue** —
   Forex ใช้ XAUUSD เท่านั้น และ Futures ใช้ MGC เท่านั้น; ห้ามใช้ชุดข้อมูลร่วมกัน
7. **fixed-risk เป็นโหมดทดลองเท่านั้น** — `--risk`/`--flat` ไม่ใช่ risk path ของ live bot;
   คำสั่ง simulator ปกติใช้ dynamic ladder + room guard เหมือน production
8. **merge `forex/bot/journal.pre-split-20260806.jsonl`** เข้าสมุดหลัก เรียงตาม `at` ตอนหยุดบอท
   ครั้งถัดไป (ตอนนี้ `--status` ยังขาด 5 closed trades)
9. `bot/forex/core/bot/state.pre-fix-20260731.json` ยังมี conflict marker ค้างจาก merge เดิม
10. ยังไม่ commit — ทุกอย่างอยู่ใน working tree

---

## 10. เช็คลิสต์ตรวจสอบ

ก่อนเปิดบัญชีฟิวเจอร์สจริง

- [ ] `python -m entrypoints.live --check` ผ่านกับ demo key และชื่อบัญชี/สัญญาถูกต้อง
- [ ] เทียบ daily loss / max loss / profit target / เวลา flat-by ของโลหะ กับ rulebook ทางการ
- [x] ดึงข้อมูล MGC Yahoo ลง `bot/future/core/test/data/market/MGC/` และให้ simulator ใช้ MGC-only
- [ ] ดึง ProjectX/TopStep MGC history เพิ่มเพื่อทำ acceptance benchmark
- [ ] ตรวจว่า stop ตาม ATR จริงกว้างพอให้ได้ 3 สัญญา ไม่งั้นการแตกสามขาจะไม่เคยทำงาน
- [ ] เขียน `run.py` และทดสอบ dry run เต็มวันเทรด
- [ ] ตั้ง `COMMISSIONED = True` เป็นขั้นตอนสุดท้าย

ข้ออ้างที่ตรวจสอบได้ (สำหรับรีวิวโค้ด)

- floor ของ trailing max loss **เดินลงไม่ได้** และ freeze ที่ยอดตั้งต้นเมื่อ EOD แตะ $52,000
- `anchored()` ปฏิเสธเทรดจนกว่า `initial_balance` จะถูกบันทึก
- `remaining_room` นับ floor ทั้งสามชั้น และ reserve หักเฉพาะชั้นที่ฆ่าบัญชี
- contract cap เลือกตามชนิดสัญญา (micro 50 / mini 5) และรวมทั้งบัญชี
- `required_target = max(3000, best_day / 0.5)` ใช้ทั้งใน `progress()` และ `stop_at_target`
- `internal_daily_stop` $400 และ `max_open_risk` $800 อยู่ใต้ DLL $1,000 พร้อม validation
- session: เสาร์ปิด · อาทิตย์เปิด 17:00 CT · ศุกร์ 15:10 ปิดยาว · จันทร์–พฤหัสค่ำเปิด
- `open_trade` จับ `ProjectXError` ด้วย บันทึก order id ก่อนยิงขาถัดไป และ timeout ≠ ไม่เกิด
- `split_contracts` แบบ largest remainder: 3→(1,1,1) · 6→(2,2,2) · 9→(3,3,3)
- `ladder_steps` = 400/300/250/200/100/50 ที่ DD 250/500/750/1000/1250/∞
- ซิมสองโหมดให้ตัวเลขตรงกันเมื่อ pin ladder ทีละ tier
- FTMO sim บังคับ 4 วันต่อเฟส และ clip ที่ internal stop 1.50%
