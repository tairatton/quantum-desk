# Dynamic Risk Bugfix Audit — 2026-07-31

## สรุป

ตรวจเส้นทาง Dynamic Risk ตั้งแต่การอ่าน configuration, high-water mark,
การเลือก risk tier, การปัด lot, Position/Pending exposure, per-idea cap,
projected internal daily stop และข้อมูลที่ดึงจาก MT5 แล้ว

สถานะรอบล่าสุด: **ผ่านการตรวจและพร้อมสำหรับ forward test แบบควบคุม**
โดยยังไม่ได้เปิดบอท LIVE หรือส่งคำสั่งซื้อขายจริงระหว่างการตรวจ

## Configuration ที่ใช้งาน

| Drawdown จาก closed-balance high-water | Risk ต่อ setup |
|---:|---:|
| ต่ำกว่า 0.50% | 1.00% |
| 0.50% ถึงต่ำกว่า 1.00% | 0.75% |
| 1.00% ถึงต่ำกว่า 1.50% | 0.50% |
| ตั้งแต่ 1.50% | 0.40% |

- Max open risk: 1.50%
- Max risk per idea: 1.50%
- Internal daily stop: 1.50%
- Max concurrent setups: 2
- Position และ Pending order ถูกนับรวมใน live exposure
- Fit remaining tiers: เปิดใช้งาน; เลือกได้เฉพาะ 1.00/0.75/0.50/0.40%

## การปรับเพื่อใช้ Risk room ที่เหลือ

เดิม setup แรกที่ Risk จริงประมาณ 1.02% ทำให้ setup ถัดไปซึ่งขอ 1.00% ถูกบล็อก
แม้ยังเหลือ room ประมาณ 0.48% รอบนี้เพิ่ม `dynamic_risk_fit_remaining` เพื่อให้
setup ถัดไปลองเฉพาะ tier ที่กำหนดไว้จากสูงไปต่ำ และเลือก 0.40% หาก Risk จริง
หลังปัด lot รวมแล้วยังไม่เกิน total, per-idea และ internal daily cap 1.50%

ผลจำลอง bootstrap 20,000 paths โดยจำกัดสอง setup และ reserve risk ทั้งวัน:

| Scenario | แบบเดิม วันรวม median/P90 | Fit tier วันรวม median/P90 | Breach เดิม → ใหม่ |
|---|---:|---:|---:|
| Holdout | 68 / 125 | 62 / 118 | 0.0% → 0.0% |
| Validation | 69 / 128 | 64 / 122 | 0.0% → 0.0% |
| Older/flat | 161 / 335 | 158 / 334 | 1.1% → 1.1% |

Older/flat two-step pass เปลี่ยนเล็กน้อยจาก 94.9% เป็น 94.8% จึงควรมองว่าเป็น
การเพิ่มการใช้ capacity ไม่ใช่การรับประกันว่าจะเร็วขึ้นทุก regime ตัวจำลองอยู่ที่
`scripts/dynamic_risk_fit_sim.py` และเป็น conservative approximation เพราะรายงาน
ไม่มีข้อมูล position overlap ระดับ tick

## บัคที่แก้ในชุดตรวจ Dynamic Risk

### 1. Daily-risk room ใช้ฐานเปอร์เซ็นต์ไม่ตรงกัน

เดิม stop จริงอ้างอิงยอดต้นวัน แต่ projected risk บางส่วนเทียบกับทุนเริ่มต้น
โดยตรง ทำให้วันที่ยอดต้นวันต่างจากทุนเริ่มต้นมีโอกาสอนุญาตความเสี่ยงเกินเส้น
จริงเล็กน้อย แก้เป็นคำนวณพื้นที่เงินสดเหนือ internal daily floor ก่อนแปลงกลับ
เป็นเปอร์เซ็นต์ของ risk basis

### 2. Floating profit ถูกนำมาเพิ่มวงเงินเสี่ยง

กำไรที่ยังไม่ปิดอาจหายไปก่อน Position ถึง SL จึงไม่ใช่วงเงินที่ใช้เปิด setup
ใหม่ได้ แก้ให้ stop risk ที่วัดจาก Entry ถึง SL ใช้ closed Balance เป็นฐาน

### 3. Floating loss ถูกนับซ้ำ

การใช้ Equity พร้อมหัก Entry-to-SL risk อีกครั้งทำให้ขาดทุนลอยตัวถูกนับสองรอบ
และบล็อก setup เร็วเกินจริง แก้ให้ใช้ Balance คู่กับ Entry-to-SL risk

### 4. Risk tier ค้างระหว่างหลาย timeframe ใน pass เดียว

ถ้า M15 เปิดก่อนแล้วค่าคอมมิชชันหรือ Equity ทำให้ DD ข้าม tier เดิม M30 อาจยัง
ใช้ tier เก่า แก้ให้ refresh Account, account health, free margin และ Dynamic Risk
ก่อนพิจารณา candidate แต่ละรายการ

### 5. Pending order ไม่ถูกนับใน per-idea cap

เดิม total exposure นับ Pending แต่ per-idea cap นับเฉพาะ Position แก้ให้แยก
BUY/SELL pending จาก `type_name` และรวมความเสี่ยงของ Pending ฝั่งเดียวกัน

### 6. Heartbeat capacity ใช้ข้อมูลไม่ครบ

Heartbeat ไม่ส่ง Balance ให้ projected daily-risk guard ทำให้ข้อความ
`ENTRY_CAPACITY` อาจต่างจาก execution guard เมื่อมี floating P/L แก้ให้หน้าจอ
สถานะและ execution ใช้ Balance/Equity ชุดเดียวกัน

### 7. ค่า NaN/Infinity ผ่าน Settings validation

Python สามารถอ่าน `NaN`/`Infinity` จาก environment หรือ JSON บางรูปแบบได้ และ
การเปรียบเทียบค่าพิเศษเหล่านี้อาจทำให้ risk cap ไม่ทำงานตามที่ตั้งใจ เพิ่ม
fail-fast validation ด้วย `math.isfinite()` สำหรับ risk tiers, DD thresholds,
exposure caps, daily/max loss และ profit target

### 8. Status capacity ไม่รู้จัก Fit Remaining

Execution สามารถลด setup ถัดไปลงเป็น tier ที่พอดีได้แล้ว แต่ status/heartbeat
ยังทดลอง projected room ด้วย tier เดิม จึงอาจแสดง `NO` ทั้งที่ execution สามารถ
ใช้ 0.40% ได้ แก้ให้ capacity preview ทดลอง configured tiers และแสดง
`next fit 0.40% nominal` โดย actual lot ยังคงถูกตรวจอีกครั้งก่อนส่งคำสั่ง

### 9. สูตร FTMO Daily Loss ใช้ opening equity และเปอร์เซ็นต์ผิดฐาน

แก้ hard floor เป็น `balance เวลา 00:00 CE(S)T - 5% ของ Initial Capital`
โดยไม่ใช้ opening equity และไม่คูณ 5% กับ balance ต้นวัน สูตร internal daily
stop ใช้หลัก fixed-initial-capital เดียวกัน

### 10. วันใหม่อ้างอิง broker midnight เร็วกว่า FTMO

FTMO ใช้ Europe/Prague แต่ MT5 server ปัจจุบันเป็น UTC+3 ทำให้ช่วงฤดูร้อน
broker midnight มาก่อน FTMO midnight หนึ่งชั่วโมง เพิ่มการแปลง timezone แบบ
DST-aware และทดสอบทั้งฤดูร้อน/ฤดูหนาวแล้ว

### 11. Restart กลางวันทำให้ยอดเที่ยงคืนกลายเป็น balance ตอนเปิดบอท

เพิ่มการย้อน net cash flow จาก deal history ของทั้งบัญชีตั้งแต่ 00:00 CE(S)T
เพื่อสร้าง midnight balance จริง รวม profit, commission, swap และ fee ของทุก
symbol/magic ไม่ใช่เฉพาะออเดอร์ของบอท

### 12. ไม่มี projected Maximum Loss guard

เพิ่ม guard ที่รวม open risk กับ setup ใหม่ก่อนส่งคำสั่ง และบล็อกหากกรณีทุก SL
ถูกชนจะต่ำกว่า static floor 90% ของ Initial Capital

### 13. Market sizing ยังเกินเพดานได้จาก slippage และการปัด lot

ทั้ง immediate และ converted market entry ใช้ stop distance ที่เผื่อ
`max_entry_slippage_r` กับ MT5 deviation และบังคับปัด lot ลงโดยไม่ยอมให้
overshoot หน้าจอ sizing, capacity, margin preview และ execution ใช้กฎเดียวกันแล้ว

### 14. Position จริงหลุดจาก state หลังโปรแกรมหยุด

พบ SELL XAUUSD สามขา tickets `509416446/509416480/509416503` มี SL/TP ที่
broker แต่ไม่มีใน `state.json` เพิ่ม startup recovery แบบ fail-closed ซึ่งรับเฉพาะ
signature ครบ TP1/TP2/TP3 และค่าทิศทาง, entry, SL, เวลาเปิดตรงกัน จากนั้นซ่อม
state ปัจจุบันสำเร็จโดยใช้ MT5 dry-run ไม่มีการแก้หรือส่งออเดอร์ไป broker

### 15. ผลปิดย้อนหลังปน loss streak ของวันใหม่

เดิมเมื่อบอทหยุดข้ามวัน `roll_day()` เริ่มวันใหม่ก่อน `reconcile_closed()` ทำให้
ผลขาดทุนที่ปิดเมื่อวานถูกเพิ่มเข้า `day_realised` และ `consecutive_losses` วันนี้
แก้ให้ใช้ deal timestamp แปลงเป็น FTMO day, นับ cash flow เฉพาะวันปัจจุบัน และ
เรียงหลายผลลัพธ์ตามเวลาปิดจริงก่อนคำนวณ loss streak

### 16. Recovery ใช้ exact fill price/time และไม่รองรับ pending

ขยาย recovery ให้รับ market legs ที่ fill ต่างราคาเล็กน้อยและห่างกันตามช่วงส่ง
สามคำสั่ง โดยยังต้องมี TP1/TP2/TP3, direction, common SL และ target ordering
ครบและไม่กำกวม เพิ่มการกู้ pending order ทั้งแบบครบและ partial placement รวมถึง
attach ticket ที่หลุดใน crash window กลับเข้า trade เดิม แทนสร้าง setup ซ้ำ

### 17. Offline fill ถูกนับเป็นวันรีสตาร์ตและเริ่ม timeout ใหม่

เก็บ deal-history fill timestamp ระหว่าง order-to-position mapping แล้วใช้เวลา fill
จริงเป็น `fill_bar_time` และ trading day ทำให้ pending ที่ fill ระหว่างบอทหยุดไม่
เพิ่มวันสอบผิดวันหรือยืด timeout 120 bars ออกจากระบบ

### 18. Stale news cache ยังเปิด entry ได้

เมื่อ cache เกิน `news_cache_hours` และ network refresh ล้มเหลว เดิม calendar ยัง
รายงาน `usable=True` ทั้งที่ production ตั้ง `news_require_calendar=true` แก้ source
เป็น `stale-cache` ซึ่งยังแสดง event เก่าให้ operator ดูได้ แต่ entry guard ปิด

### 19. Dashboard และ forward evidence ใช้ฐานข้อมูลไม่ครบ

แก้ Daily room ให้เทียบเงินเหนือ hard floor กับ Initial Capital และให้
`forward_check.py` อ่าน risk cash จาก orphan recovery events เพื่อคำนวณต้นทุน/R
ของไม้ที่ state ถูกกู้กลับมา

## Regression tests ที่เพิ่ม

- การลดและเพิ่ม risk tier ตาม DD
- high-water mark คงอยู่หลัง restart
- cap ต้องรองรับ tier สูงสุด
- reject ค่า NaN/Infinity
- Pending risk รวมใน total exposure
- แยก Pending BUY/SELL สำหรับ per-idea cap
- ยอดต้นวันต่ำและสูงกว่าทุนเริ่มต้น
- floating profit ไม่เพิ่ม daily room
- floating loss ไม่ถูกนับซ้ำ
- ใช้ความเสี่ยงจริงหลังปัด lot
- fit remaining เลือกเฉพาะ configured tier และไม่เกิน 1.50%

## ผลยืนยันล่าสุด

| รายการ | ผล |
|---|---:|
| Full unit/regression suite | 271 tests + 19 subtests ผ่าน |
| Python compile | ผ่าน |
| `git diff --check` | ผ่าน |
| MT5 `--status` dry-run | ผ่าน |
| FTMO portfolio smoke simulation | ผ่าน (2,000 paths, XAU M15+M30, BE 33/33/34) |

สถานะ MT5 ขณะตรวจ:

- Account: FTMO-Demo 1514115848, Hedging
- Balance: $50,435.50
- Midnight balance 00:00 CE(S)T: $50,435.77
- Equity ณ การตรวจรอบสุดท้าย: $50,381.60 (เปลี่ยนตามราคา)
- High-water: $50,435.50
- Dynamic DD ณ การตรวจรอบสุดท้าย: 0.11%
- Risk tier: 1.00%
- Position: 3 ขาของ setup เดียว, open risk รวมประมาณ 0.39%
- Pending: 0
- Entry capacity: YES ภายใต้ guardrails ปัจจุบัน
- Trading days: 3/4
- Broker-side SL/TP: ครบทั้งสาม position (`PROTECTED`)

ไฟล์สำรองก่อนซ่อม state:

- `bot/code/state.pre-fix-20260731.json`
- `bot/code/state.pre-live-repair-20260731.json`

## จุดที่ยังต้องตัดสินใจเชิงกลยุทธ์

Status monitor ยังแจ้งว่า M15 backtest report เลือก `fixed_tp3` แต่ production
profile ที่ balance $50,000 ใช้ `capital_tier` ซึ่ง resolve เป็น `be_33_33_34`
จุดนี้ไม่ใช่ runtime bug ของ Dynamic Risk แต่เป็นความไม่ตรงกันระหว่าง exit policy
ที่รายงานแนะนำกับ policy ที่เปิดใช้งานอยู่ ไม่ควรเปลี่ยนโดยไม่มี backtest/forward
comparison เพราะจะเปลี่ยนผลลัพธ์ของระบบโดยตรง

## ข้อจำกัดของผลตรวจ

การผ่าน unit tests และ dry-run ยืนยัน logic และ integration ที่จำลอง/อ่านได้ แต่
ไม่รับประกันผลกำไรหรือการผ่าน FTMO การเปิด LIVE ควรเริ่มด้วย forward monitoring
และตรวจ journal, actual fill, commission, slippage และ daily reset อย่างต่อเนื่อง

ผล smoke simulation แบบ fixed 1.00% ไม่ใช่ forecast ของ Dynamic Risk จริง เพราะ
production จำกัด open/internal daily risk 1.50% และจะ fit setup ถัดไปลงตาม room
ที่เหลือ จึงใช้ผลดังกล่าวเพื่อตรวจ pipeline เท่านั้น ไม่ใช้สรุปโอกาสผ่าน
