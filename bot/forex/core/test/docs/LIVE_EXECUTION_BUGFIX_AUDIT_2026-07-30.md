# รายงานตรวจสอบและแก้ไขระบบ LIVE Execution

วันที่ตรวจสอบ: 30 กรกฎาคม 2026  
ระบบ: Quantum Desk, XAUUSD, M15 + M30, FTMO Demo  
ขอบเขต: state persistence, market-fill reconciliation, break-even, commission,
slippage, swap, fast management polling และ closed-trade reconciliation

## สรุปผล

ตรวจพบและแก้ไขบัคในเส้นทาง LIVE execution รวม 7 กลุ่ม:

1. Windows ปฏิเสธการแทนที่ `state.json` ชั่วคราวและทำให้ process หยุดหลังส่ง
   market orders สำเร็จแล้ว
2. Break-even เดิมใช้ราคา entry ของสัญญาณ แทนราคา fill จริงของแต่ละ position
3. SL ที่ราคา fill จริงยังขาดทุนสุทธิจาก commission และ slippage
4. ตัวแทน position และ Floating P/L เดิมไม่รวม cumulative swap จาก MT5
5. Fast polling เดิมหยุดหลังตั้ง BE สำเร็จ ทำให้ negative swap ที่เกิดภายหลัง
   ไม่ถูกนำไปเพิ่ม buffer
6. Position ที่ปิดระหว่างบอทหยุดยังค้าง setup slot หลัง startup จนถึง signal
   pass ถัดไป
7. Position ที่ปิดระหว่าง fast polling ยังค้าง setup slot จนถึงแท่ง M15/M30
   ถัดไป

หลังแก้ไข:

- ใช้ราคา `price_open` จริงราย position
- เพิ่ม commission, slippage และ negative swap ใน net break-even
- Positive swap ไม่ทำให้ SL ถอยกลับ
- Refresh swap-aware SL ทุก 3 นาทีระหว่างที่ split trade ยังเปิด
- Floating P/L แสดงกำไรจากราคา + cumulative swap
- Startup และ fast loop สรุป closed trade พร้อมคืน setup slot ทันที
- Atomic state save retry เฉพาะ transient `PermissionError` แบบมีขอบเขต
- ชุดทดสอบทั้งหมดผ่าน `190 passed, 5 subtests passed`

## เหตุการณ์และสาเหตุหลัก

### 1. State save ล้มเหลวหลัง broker รับ order แล้ว

Broker รับ market orders ครบสามขา แต่ Windows คืน `WinError 5` ตอน
`os.replace(state.json.tmp, state.json)` ทำให้ process หยุด ข้อมูล order tickets
ทั้งสามยังถูกบันทึกใน durable state ก่อนเกิด failure จึงสามารถ recover โดยไม่ส่ง
order ซ้ำได้

แก้ใน [`bot/code/state.py`](../bot/code/state.py):

- เขียนและ `fsync` temporary snapshot ก่อน
- Retry atomic replace ด้วย bounded backoff เมื่อพบ `PermissionError`
- หาก lock ไม่หายภายในช่วง retry ยังคง fail loudly และเก็บ temporary snapshot
  ไว้ ไม่กลืนข้อผิดพลาด

### 2. Break-even ใช้ signal entry ผิดราคา

ค่าที่พบจากออเดอร์จริง:

| ขา | Signal entry | Broker fill | SL เดิม |
|---|---:|---:|---:|
| TP2 | 4088.77 | 4091.30 | 4088.77 |
| TP3 | 4088.77 | 4091.27 | 4088.77 |

SL เดิมต่ำกว่า broker fill ประมาณ 2.5 ดอลลาร์ จึงไม่ใช่ break-even ของ position
จริง หากปิดตรง SL เดิม สองขาที่เหลือจะขาดทุนด้านราคาประมาณ 12.56 ดอลลาร์
ก่อน commission

แก้ใน [`bot/code/trader.py`](../bot/code/trader.py):

- คำนวณ SL แยกจาก `position.price_open` ของแต่ละขา
- ปัดราคาไปด้านกำไรเสมอ
- ไม่ใช้ `trade.entry` เป็น broker break-even อีก

### 3. Gross break-even ไม่ใช่ net break-even

ระบบใช้ cost model เดียวกับ technique lab:

```text
base_buffer_price =
    round_trip_commission_per_lot / value_per_price_unit_per_lot
    + expected_stop_slippage_price
```

สำหรับ XAUUSD:

```text
commission buffer = 7 / 100 = 0.07
slippage buffer   = 0.05
base buffer       = 0.12
```

ดังนั้นก่อนมี swap:

```text
BUY  net BE = actual fill + 0.12
SELL net BE = actual fill - 0.12
```

ค่าปัจจุบันตั้ง commission model ไว้สูงกว่าค่าที่สังเกตจาก deal จริงเล็กน้อย
เพื่อไม่ประเมินต้นทุนต่ำเกินไป

## การรองรับ Swap

MT5 รายงาน `POSITION_SWAP` เป็น cumulative cash ในสกุลเงินบัญชีแยกจาก
`POSITION_PROFIT` ระบบจึงอ่าน `swap` ลงใน
[`Position`](../bot/code/broker.py) โดยตรง

สูตรต่อ position:

```text
negative_swap_cash = max(0, -position.swap)

swap_buffer_price =
    negative_swap_cash
    / (value_per_price_unit_per_lot * position.volume)

total_buffer_price =
    commission_buffer
    + slippage_buffer
    + swap_buffer_price

BUY SL  = actual fill + total_buffer_price
SELL SL = actual fill - total_buffer_price
```

ตัวอย่าง TP2 ขนาด 0.02 lot และ swap เท่ากับ -0.40 ดอลลาร์:

```text
swap buffer = 0.40 / (100 * 0.02) = 0.20
total buffer = 0.12 + 0.20 = 0.32
SL = 4091.30 + 0.32 = 4091.62
```

นโยบาย:

- Negative swap ทำให้ SL ถูก tighten เพิ่ม
- Positive swap ไม่ถูกใช้ลด buffer และไม่ทำให้ SL ถอยกลับ
- Fast management ตรวจใหม่ทุก 3 นาที
- ถ้า broker ปฏิเสธ SL ใหม่ ระบบตั้ง `breakeven_done=False`, บันทึก
  `breakeven_rejected`, แสดง `BE_STOP_BELOW_NET` และ retry
- เมื่อ request usage ถึง 90% ของ daily cap ระบบหยุด fast polling เพื่อไม่ชน
  terminal request limit และกลับไปจัดการใน signal pass

## Monitoring ที่แก้ไข

แก้ใน [`bot/code/run.py`](../bot/code/run.py):

- Floating P/L ใช้ `position.profit + position.swap`
- Position output แยก `Gross`, `Swap`, `Net`
- `BE_STOP_BELOW_FILL`: SL แย่กว่า broker fill
- `BE_STOP_BELOW_NET`: SL สูง/ต่ำไม่พอครอบคลุมต้นทุนหรือ swap ล่าสุด
- `SL_AT_GROSS_BE`: เท่าราคา fill แต่ยังไม่ครอบคลุมต้นทุน
- `SL_AT_NET_BE`: ถึง cost- and swap-aware break-even แล้ว

## Closed-trade reconciliation

แก้สองเส้นทาง:

1. `reconcile_startup()` เรียก `reconcile_closed()` ก่อนโหลด chart bars เพื่อ
   สรุป position ที่ปิดตอนบอทหยุด แม้ price-history channel มีปัญหา
2. Fast management ใช้ live-ticket snapshot ที่อ่านเพื่อ BE อยู่แล้ว หากไม่พบ
   ticket เหลือ จะเรียก `reconcile_closed()` ทันที ไม่รอแท่งถัดไป

ผลคือ setup capacity, daily realised P/L และ loss streak ไม่ค้างหลัง position
ปิด

## ผลยืนยันจากออเดอร์จริง

| ขา | Exit | ผลสุทธิหลัง commission/swap |
|---|---:|---:|
| TP1 | TP fill 4117.32 | +79.92 |
| TP2 | SL 4091.42, fill 4091.50 | +0.28 |
| TP3 | SL 4091.39, fill 4091.50 | +0.51 |
| รวม |  | **+80.71** |

ข้อมูลสรุป:

- Commission รวม: -0.48
- Swap ของรายการนี้: 0.00
- ผลลัพธ์: +0.3818R
- Trade ถูกบันทึก `closed=True`
- Startup รายงาน `managed_trades=0`
- Entry capacity กลับเป็น `setups 0/2`
- ไม่มี position หรือ pending order ค้าง
- Balance และ equity ขณะ audit เท่ากันที่ 50,435.77

## Regression tests

เพิ่มหรือปรับการทดสอบสำหรับ:

- transient Windows atomic-replace lock
- market fill ต่างจาก signal entry
- cost-covered BUY และ SELL break-even
- negative swap tighten SL หลัง BE สำเร็จไปแล้ว
- positive swap ห้าม loosen SL
- Floating P/L รวม negative swap
- health status ตรวจ cost/swap buffer ล่าสุด
- fast polling refresh swap โดยไม่ query ticket history ที่ไม่จำเป็น
- startup สรุป position ที่ปิดระหว่าง downtime
- fast loop สรุป survivor ที่ปิดระหว่าง signal bars
- partial SL modification rejection และ retry

คำสั่งตรวจสอบ:

```powershell
python -m pytest -q
```

ผลล่าสุด:

```text
190 passed, 5 subtests passed
```

## ผลตรวจซ้ำหลังปรับปรุงรอบที่ 2

ตรวจพบและแก้ไข edge case เพิ่มเติม:

1. `apply_breakeven()` ในโหมด dry-run เคยคืน live-ticket snapshot เป็นชุดว่าง
   เพื่อป้องกันไม่ให้มีการขยับ SL จริง แต่ fast management loop ใช้ snapshot เดียวกัน
   ตรวจว่า position ปิดหมดแล้วหรือไม่ จึงมีโอกาสเรียก `reconcile_closed()` ผิดจังหวะ
   ทั้งที่ MT5 ยังแสดง position จริงอยู่
2. แก้ให้ dry-run อ่านและคืน ticket ของ position จริง แต่ยังออกจากฟังก์ชันทันที
   ก่อนการแก้ SL หรือเปลี่ยน durable trade state จึงยังคงไม่มี broker write
3. เพิ่ม validation ว่า `split_management_poll_seconds` ต้องมากกว่า 0
   ป้องกันการตั้งค่าเป็นศูนย์แล้ววน query MT5 อย่างรวดเร็วจนชน daily request limit
4. เพิ่ม regression test สำหรับ SELL ที่มี negative swap เพื่อยืนยันทิศทางว่า SL
   ต้องเลื่อนลงหากำไร ไม่ใช่เลื่อนขึ้นไปทางขาดทุน

ผลตรวจหลัง deploy:

- Full suite: `185 passed in 5.62s`
- `git diff --check`: ผ่าน
- LIVE trading process หลัง final restart: PID `17404` เพียงหนึ่ง process
- Startup sync: managed trades 0, positions 0, pending orders 0
- Entry capacity: setups 0/2
- Day realised: +124.36
- ไม่พบ duplicate order หรือการเปิด order ระหว่าง restart

## ผลตรวจซ้ำหลังปรับปรุงรอบที่ 3

พบ fail-safe bug เพิ่มเติมใน MT5 read path:

1. MetaTrader5 Python API แยกผลลัพธ์สองแบบชัดเจน:
   - empty tuple/sequence หมายถึงอ่านสำเร็จแต่ไม่มี position, order หรือ history
   - `None` หมายถึงเกิดข้อผิดพลาดและต้องอ่านรายละเอียดจาก `last_error()`
2. โค้ดเดิมใช้ `result or ()` ทำให้ `None` ถูกแปลงเป็นรายการว่าง หากเกิด IPC timeout
   ระหว่าง fast management loop ระบบจึงมีโอกาสเข้าใจผิดว่า position ปิดหมดแล้ว
   และเริ่ม reconciliation จากข้อมูลที่ไม่สมบูรณ์
3. แก้ให้ `positions_get()`, `orders_get()`, `history_deals_get()` และ
   `history_orders_get()` ตรวจ `None` และ raise `MT5Error` พร้อม error code
   เพื่อให้ main loop reconnect โดยไม่เปลี่ยน trade state
4. ยืนยันด้วย regression tests ว่า `None` ทั้ง 5 read paths ถูกปฏิเสธ แต่ empty tuple
   ยังคืนค่า no exposure/history ตามปกติ
5. แก้ป้ายค่า position จาก `Net` เป็น `Gross+Swap` เพราะค่าระหว่างถือ position
   ยังไม่มี commission ราย position จาก deal history ส่วน daily-loss guard ใช้ Equity
   ของ MT5 โดยตรงและผลปิด trade ใช้ profit + commission + swap ครบถ้วนอยู่แล้ว

ผลตรวจและ deploy รอบที่ 3:

- Targeted tests: `112 passed, 5 subtests passed`
- Full suite: `187 passed, 5 subtests passed in 4.62s`
- Python compile check: ผ่าน
- MT5 ก่อน restart: positions 0, pending 0, balance/equity 50,435.77
- LIVE process หลัง restart: PID `9712` เพียงหนึ่ง process
- Startup sync: managed trades 0, positions 0, pending orders 0
- Entry capacity: setups 0/2
- ไม่พบ duplicate order ระหว่าง restart

## ผลตรวจซ้ำหลังปรับปรุงรอบที่ 4

พบและแก้ไขเพิ่ม 2 กลุ่ม:

1. Write paths หลายจุดจับ `MT5Error` กว้างเกินไป ทั้ง broker rejection และ
   transport/IPC failure จึงถูกบันทึกเป็น `*_REJECTED` เหมือนกัน ผลคือกรณี
   `order_send()` ไม่คืนผลเพราะ connection ขาด อาจไม่ส่ง exception ไปยัง main loop
   เพื่อ reconnect
2. แก้ `open_trade`, break-even stop modification, timeout close, stale-order cancel
   และ emergency flatten ให้จับเฉพาะ `OrderRejected` ส่วน `MT5Error` จาก terminal,
   IPC หรือ connection จะ propagate ไปยัง reconnect handler
3. Local stop-widening guard เปลี่ยนเป็น `OrderRejected` เพื่อยังคงเป็น policy refusal
   และไม่กระตุ้น reconnect โดยไม่จำเป็น
4. Closed-deal net เดิมรวม profit + commission + swap แต่ยังไม่รวม `DEAL_FEE`
   แก้ broker mapping และ reconciliation costs/R ให้รวม `fee` ด้วย
5. เปลี่ยนข้อความใน position description จาก `net` เป็น `gross_plus_swap`
   ให้ตรงกับข้อมูลระหว่างถือ position ซึ่งยังไม่ใช่ผลสุทธิหลัง commission/fee

Regression coverage ที่เพิ่ม:

- transport failure ระหว่างขยับ break-even ต้อง propagate และห้ามตั้ง
  `breakeven_done=True`
- broker rejection ยังคง retry เฉพาะ ticket ที่ถูกปฏิเสธโดยไม่หยุด ticket อื่น
- closed-deal mapping และ trade R ต้องรวม MT5 fee

ผลตรวจและ deploy รอบที่ 4:

- Targeted tests: `114 passed, 5 subtests passed`
- Full suite: `189 passed, 5 subtests passed in 4.71s`
- Python compile check: ผ่าน
- MT5 ก่อน restart: positions 0, pending 0, balance/equity 50,435.77
- LIVE process หลัง restart: PID `14204` เพียงหนึ่ง process
- Startup sync: managed trades 0, positions 0, pending orders 0
- Entry capacity: setups 0/2
- ไม่พบ duplicate order ระหว่าง restart

## ผลตรวจซ้ำหลังปรับปรุงรอบที่ 5

ตรวจ reconnect และ request accounting หลังแยก `OrderRejected`/`MT5Error` พบ
persistence edge case เพิ่มเติม:

1. Broker นับ request ที่ส่งไปแล้วไว้ใน memory และปกติจะย้ายยอดเข้า
   `state.day_requests` ตอนจบ pass
2. หากเกิด rejection, IPC timeout หรือ read/write connection error ก่อนถึงท้าย pass
   request เหล่านั้นยังอยู่ใน process memory และจะถูกบันทึกเมื่อ pass ถัดไปสำเร็จ
   แต่ถ้า process ถูกปิดระหว่าง outage ยอดดังกล่าวจะหายจาก `state.json`
3. แก้ด้วย `checkpoint_state()` ซึ่งย้าย `broker.take_requests()` เข้า durable state
   และบันทึกแบบ atomic ทันที
4. ใช้ checkpoint เดียวกันทั้ง normal pass, fast split management, startup sync,
   broker rejection, connection loss และ post-reconnect reconciliation failure
5. เพิ่ม regression test จำลอง `positions_get()` เกิด IPC timeout หลังใช้ 7 requests
   แล้วยืนยันว่าก่อน reconnect ทั้ง in-memory state และ state file บันทึกครบ 7 requests

การแก้รอบนี้ไม่เปลี่ยน signal, sizing, SL, TP, break-even buffer หรือ swap formula
แต่ทำให้ daily request limit ยังคงถูกต้องแม้ bot หยุดระหว่าง connection outage

ผลตรวจและ deploy รอบที่ 5:

- Targeted tests: `115 passed, 5 subtests passed`
- Full suite: `190 passed, 5 subtests passed in 4.50s`
- Python compile check: ผ่าน
- `git diff --check`: ผ่าน
- MT5 ก่อน restart: positions 0, pending 0, balance/equity 50,435.77
- LIVE process หลัง restart: PID `24668` เพียงหนึ่ง process
- Startup sync: managed trades 0, positions 0, pending orders 0
- Durable request count หลัง startup: 786
- Entry capacity: setups 0/2
- ไม่พบ duplicate order ระหว่าง restart

## ผลตรวจซ้ำหลังปรับปรุงรอบที่ 6

รอบนี้ตรวจ atomic checkpoint, day rollover และ restart idempotency หลังการแก้รอบที่ 5
โดยไม่พบ production bug ใหม่และไม่มีการแก้ trading code เพิ่ม

รายการที่ยืนยัน:

1. `BotState.save()` เขียน JSON ลง temporary file, flush + `fsync()` แล้วใช้
   `os.replace()` พร้อม bounded retry เมื่อ Windows ล็อก destination ชั่วคราว
2. `checkpoint_state()` รับยอดจาก `broker.take_requests()` ก่อน atomic save
   จึงไม่บันทึก request ซ้ำในการ checkpoint ครั้งถัดไป
3. เมื่อ broker server day เปลี่ยน `roll_day()` reset request count ก่อน checkpoint
   ของ pass วันใหม่ ทำให้ request ที่เกิดใน pass ใหม่นับเข้าวันใหม่
4. Dry-run ปิด persistence จึงไม่สามารถ replace production `state.json`
5. LIVE instance lock ถูกทดสอบด้วยการพยายามเริ่ม `python -m entrypoints.main` ตัวที่สอง
   และถูกบล็อกทันทีด้วยข้อความ
   `another Quantum Desk LIVE process is already running`
6. State และ journal ไม่มี open managed trade, connection error, rejected write
   หรือ duplicate order หลัง deploy รอบก่อน

ผลตรวจรอบที่ 6:

- Full suite: `190 passed, 5 subtests passed in 4.91s`
- `git diff --check`: ผ่าน
- MT5 read-only audit: positions 0, pending 0, balance/equity 50,435.77
- LIVE process: PID `24668` เพียงหนึ่ง process
- Durable request count: 786
- Entry capacity: setups 0/2
- ไม่ต้อง restart เพราะไม่มี production code เปลี่ยนในรอบนี้

## ข้อจำกัดที่ยังมี

1. Stop Loss เป็น trigger price ไม่ใช่ guaranteed execution price หากเกิด gap
   หรือ slippage เกิน model ผลสุทธิยังอาจติดลบได้
2. Swap refresh มีช่วงห่างสูงสุด 3 นาที และอาจถูกลดความถี่เมื่อ request budget
   ใกล้ 90%
3. Commission/slippage buffer เป็น conservative model ไม่ใช่การรับประกันต้นทุน
   ในอนาคต หาก broker เปลี่ยนค่าธรรมเนียมต้องปรับ `xau/config.py`
4. หาก negative swap ทำให้ desired SL ข้าม current executable price broker อาจ
   ปฏิเสธการแก้ไข ระบบจะ alert และ retry แต่จะไม่ปิด market เอง เพราะการปิดทันที
   เป็น exit policy ใหม่ที่ยังไม่ได้ผ่าน backtest
5. หน้าต่าง console เก่าที่หยุดจาก controlled restart ไม่มี process ซื้อขายแล้ว
ปิดได้ โดยตรวจให้เหลือ `python -m entrypoints.main` เพียง process เดียว

## เอกสารอ้างอิง MT5

- [Position properties: open price, SL, cumulative swap และ current profit](https://www.mql5.com/en/docs/constants/tradingconstants/positionproperties)
- [Python `positions_get()` และฟิลด์ `swap`](https://www.mql5.com/en/docs/python_metatrader5/mt5positionsget_py)
- [Python `orders_get()` และการคืน `None` เมื่อเกิด error](https://www.mql5.com/en/docs/python_metatrader5/mt5ordersget_py)
- [Python `history_deals_get()` และการคืน `None` เมื่อเกิด error](https://www.mql5.com/en/docs/python_metatrader5/mt5historydealsget_py)
- [Deal properties: commission, swap, profit และ fee](https://www.mql5.com/en/docs/constants/tradingconstants/dealproperties)
- [Python `order_send()` และความหมายของ Stop Loss activation price](https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py)

## สถานะสุดท้ายของ Audit

- Runtime state สอดคล้องกับ MT5
- ไม่มี open position
- ไม่มี pending order
- ไม่มี duplicate order
- ไม่มี stale managed trade
- LIVE instance lock ทำงานและมี process ซื้อขายเพียงตัวเดียว
- Test suite ผ่านทั้งหมด
