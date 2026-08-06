# เงื่อนไข FTMO ทั้งหมด และระบบนี้ทำตามตรงไหน

ตรวจจากเว็บ FTMO เมื่อ 2026-07-26 · แหล่งอ้างอิงอยู่ท้ายเอกสาร
**ตัวเลขในโค้ดเป็นค่าที่บันทึกไว้ ณ วันนั้น ไม่ได้ดึงสด ต้องเช็คซ้ำก่อนซื้อทุกครั้ง**

---

## 1. Trading Objectives

| เงื่อนไข | **2-Step** (ที่เราเลือก) | **1-Step** |
|---|---|---|
| Profit target | 10% (Challenge) → 5% (Verification) | 10% |
| Max daily loss | **5%** ของทุนเริ่มต้น | **3%** ของทุนเริ่มต้น |
| Max loss | **10% แบบ static** | **10% แบบ end-of-day trailing** จาก balance สูงสุดที่เคยทำได้ |
| Minimum trading days | **4 วัน** (ทั้ง Challenge และ Verification) | ไม่มี |
| Best Day Rule | ไม่มี | มี — วันที่ดีสุดต้องไม่เกิน 50% ของกำไรวันบวกรวม |
| Time limit | **ไม่มี** | **ไม่มี** |
| Profit target บนบัญชี funded | ไม่มี | ไม่มี |

### วิธีคิดที่ต้องเข้าใจให้ตรง

- **Max daily loss คิดจาก equity ไม่ใช่ balance** — รวมกำไร/ขาดทุนลอยของไม้ที่ยังเปิดอยู่
  ("Equity cannot drop below this limit") คำนวณใหม่ทุกวันเวลา **00:00 CE(S)T**
- **Max loss ของ 2-Step เป็น static** วัดจากทุนเริ่มต้นตายตัว ไม่ไล่ตามกำไร
- **Max loss ของ 1-Step เป็น trailing แบบ end-of-day** ขึ้นตาม balance สูงสุด ลงไม่ได้
  → ทำกำไรได้ 6% แล้วเส้นตายขยับขึ้นมาที่ −4% จากจุดนั้น
- Minimum trading day = วันตามเวลา 00:00:00–23:59:59 CE(S)T ที่ **เปิดไม้อย่างน้อย 1 ไม้**

### สองเหตุผลใหม่ที่ยืนยันว่าต้องเลือก 2-Step

1. **Max loss ของ 1-Step เป็น trailing** — ระบบนี้มี drawdown ในอดีตสูงสุด 15–22R
   ถ้าเส้นตายไล่ตามกำไร โอกาสชนสูงกว่ามาก
2. **Best Day Rule มีแค่ใน 1-Step** และ FTMO ระบุว่าการ "partially closing and managing
   the same trade idea across multiple trading days" เพื่อเลี่ยง Best Day Rule เป็นสิ่งห้าม
   → exit แบบแบ่ง 3 leg TP1/TP2/TP3 ของเรา อาจข้ามวันได้ ซึ่งบน 2-Step ไม่มีกฎนี้ให้เลี่ยง
   จึงไม่มีประเด็น แต่บน 1-Step เป็นความเสี่ยงที่ไม่จำเป็นต้องแบก

### เวลาและการหยุดพัก

- ไม่มีลิมิตเวลาแล้ว (FTMO ยกเลิกเพดาน 30/60 วันไปแล้ว) → สอบช้าได้ ไม่เสียเงินซ้ำ
- ทิ้งบัญชีไว้เฉย ๆ หลายสัปดาห์ FTMO จะติดต่อมา และขอ **freeze** บัญชีได้ถ้ารู้ล่วงหน้าว่าจะหยุดยาว
  — ไม่ใช่การผิดกฎ แต่ก็ไม่ควรปล่อยเงียบ

---

## 2. Forbidden Trading Practices

| ข้อห้าม | นิยามของ FTMO | ระบบนี้เกี่ยวไหม |
|---|---|---|
| ใช้ error ของระบบ | เทรดจากราคาที่แสดงผิดหรือ feed ค้าง | ไม่ |
| เปิดไม้สวนทางข้ามบัญชี | เปิด opposite positions พร้อมกันเพื่อ manipulate (ยกเว้นในบัญชีเดียว) | ไม่ (บัญชีเดียว) |
| **Hedging บนสัญลักษณ์เดียวกัน** | ถือไม้สวนทางบนสัญลักษณ์เดียวกันหรือที่สัมพันธ์กันสูง | **เกี่ยว** — M15 กับ M30 อาจให้สัญญาณสวนกัน |
| **HFT / server requests** | ห้าม EA ยิงเกิน **2,000 requests/วัน** (และ 200 orders พร้อมกัน) | **เกี่ยว** — ลูปเดิม poll ทุก 15 วิ = เกินลิมิต |
| **Gap trading** | เปิดไม้ภายใน **2 ชั่วโมงก่อนตลาดปิด** ที่ปิดยาว 2 ชม.+ หรือรอบข่าวใหญ่ | **เกี่ยว** — ปิดศุกร์ **และวันหยุด** (คริสต์มาส/ปีใหม่ ปิด 2 ชม.–4 วัน) |
| **Risk management ไม่สม่ำเสมอ** | ขนาดไม้ใหญ่/เล็กกว่าปกติอย่างมีนัย, จำนวนไม้ไม่นิ่ง, "higher Risk per Trade Idea" ซ้ำ ๆ | **เกี่ยว** — ต้องคุม risk/ไม้ให้คงที่และคุม exposure ต่อ 1 ไอเดีย |
| ให้คนอื่นเข้าบัญชี | ห้าม third party เข้าใช้บัญชี และห้ามเทรดบัญชีคนอื่น | ไม่ |
| EA / บอท | **อนุญาต** แต่บอทของคนอื่นที่ใช้กันเยอะอาจโดนจำกัดวงเงิน | ไม่ (บอทเขียนเอง) |

**บทลงโทษ:** ลบประวัติเทรด, จำกัดการเข้าแพลตฟอร์ม, ตัดสิทธิ์การประเมิน, ยึดรางวัล
หรือยกเลิกสัญญาทั้งหมด

หลักการรวบยอดของ FTMO: กลยุทธ์ต้อง *"replicable on live accounts to generate the same
results"* และต้องเป็นการเทรดจริงจัง ไม่ใช่การพนัน

---

## 3. กฎที่เพิ่มเข้ามาตอนได้บัญชี funded (ยังไม่ใช้ตอนสอบ)

| เงื่อนไข | Standard | Swing |
|---|---|---|
| เทรดข่าว | **ห้ามเปิด/ปิด/ตั้ง pending ในช่วง −2 ถึง +2 นาที** รอบข่าวที่กำหนด | ไม่จำกัด |
| ถือข้ามคืน / ข้ามสุดสัปดาห์ | **มีข้อจำกัด** (เฉพาะตอน funded ไม่ใช่ตอนสอบ) | ไม่จำกัด |
| Leverage ทั่วไป | สูงสุด 1:100 | สูงสุด 1:30 |
| Leverage โลหะ (ทอง) | 1:30 | 1:9 |

**ตอน Challenge และ Verification ไม่มีข้อจำกัดเรื่องข่าวและการถือข้ามคืนเลย** ทั้งสองประเภทบัญชี

**ประเด็นสำคัญสำหรับระบบนี้:** ทอง M15/M30 ถือไม้ข้ามคืนเป็นปกติ และ timeout ที่ 120 แท่ง
M30 = 60 ชั่วโมง = ข้ามสุดสัปดาห์ได้ → **ถ้าผ่านไปถึง funded ต้องเลือก Swing account**
ไม่ใช่ Standard มิฉะนั้นจะถูกบังคับปิดไม้ก่อนสุดสัปดาห์ซึ่งไม่ใช่ exit ที่ backtest วัดไว้

เช็ค margin ที่ leverage ต่ำสุด (Swing metals 1:9): บัญชี \$100k ถือทอง 0.24 lot
ที่ราคา \$4,000 → notional \$96,000 → margin \$10,667 ใช้ไป 10.7% ของบัญชี ยังปลอดภัย

---

## 4. ระบบบังคับกฎไว้ที่ไหน

| กฎ | บังคับที่ | ค่าเริ่มต้น |
|---|---|---|
| Max loss 10% static | [guardrails.py](../bot/code/guardrails.py) `account_health` → หยุดถาวรใน `state.json` | 10.0 |
| Max daily loss 5% (จาก equity) | `account_health` เทียบ equity กับ **ค่าที่สูงกว่าระหว่าง balance/equity ต้นวัน** (`state.day_start_equity`) ตามที่ FTMO คิด | 5.0 |
| วันเทรดตัดที่ 00:00 CE(S)T | `state.roll_day` ใช้ **เวลา server** จาก tick ไม่ใช่เวลาเครื่อง | – |
| Minimum 4 trading days | `state.count_trading_day` — นับทั้ง market entry และ limit ที่ fill แล้ว | 4 |
| **หยุดเมื่อสอบผ่าน** | `account_health` หยุดเปิดไม้เมื่อถึง target **และ** ครบ 4 วัน — เทรดต่อไม่มีรางวัล มีแต่ความเสี่ยง | `stop_at_target=True` |
| ห้าม hedge สัญลักษณ์เดียวกัน | `no_opposing_position` → ทิ้งสัญญาณที่มาสวนไม้ที่เปิดอยู่ | เปิดใช้ |
| Risk per trade idea | `risk_per_idea` คุม exposure ทางเดียวกันแยกจาก cap รวม (คิดจาก **initial balance**) | 0.80% |
| Gap trading ก่อนตลาดปิด | [market_hours.py](../bot/code/market_hours.py) หา "การปิดครั้งถัดไป" จาก weekly close **+ วันหยุดใน `market_closures`** → `entry_window_open` กัน 3 ชม. · ปิดสั้นกว่า 2 ชม. ไม่เข้าข่าย | 3.0 ชม. |
| News blackout | [news.py](../bot/code/news.py) ดึงปฏิทินอัตโนมัติ → `entry_window_open` · [NEWS_GUARD.md](NEWS_GUARD.md) | **−5 / +3 นาที**, USD, high impact |
| ≤2,000 server requests/วัน | `broker.requests` **นับจริงทุก call** → `state.day_requests` (รอด restart) → `can_open` หยุดเปิดไม้ที่ 90% ของโควตา | 2000 |
| Margin เพียงพอ | `margin_available` ถาม `order_calc_margin` ก่อนส่ง — กันการถูกปฏิเสธกลางการวาง 3 leg | ≤80% ของ margin_free |
| Risk ต่อไม้คงที่ | [sizing.py](../bot/code/sizing.py) คิด lot จากระยะ SL ทุกไม้ ไม่เคยเพิ่มหลังแพ้ · คิดจาก **initial balance** ไม่ใช่ balance ปัจจุบัน จึงไม่ไล่ตามกำไร | 0.40% |
| SL แคบกว่าที่โบรกเกอร์ยอม | `size_plan` ปฏิเสธถ้า stop < `trade_stops_level` | – |
| ห้ามขยาย SL | `broker.move_stop` โยน error ถ้าทิศทางไม่ใช่การขยับเข้าหากำไร | – |

### สิ่งที่พบว่าไม่ตรงกฎ และแก้แล้ว

1. **ลูปยิง request เกินลิมิต** — เดิม poll ทุก 15 วินาที = 11,000+ calls/วัน เทียบลิมิต 2,000
   แก้เป็นนอนรอถึงเวลาแท่งปิดจริง เหลือประมาณ 800 calls/วัน
2. **ไม่มีด่านกัน hedge** — M15 long กับ M30 short เปิดพร้อมกันได้ ซึ่งเป็นข้อห้ามตรง ๆ
   เพิ่ม `no_opposing_position`
3. **ไม่มีด่านกัน gap trading** — เปิดไม้เย็นวันศุกร์ก่อนปิดได้ เพิ่ม `entry_window_open`
4. **ไม่ได้นับ minimum 4 trading days** — เพิ่ม `state.trading_days` และรายงานใน `--status`
5. **การแบ่ง lot บิดเบี้ยว** — 0.09 lot เคยออกมา 0.02/0.02/0.05 (22/22/56) ทำให้ exit
   ไม่ตรงกับที่วัดผล เปลี่ยนเป็น largest-remainder ได้ 0.03/0.03/0.03

6. **ปฏิทินข่าวไม่มีเลย** — มีแต่ช่องพิมพ์เวลาเอง สร้าง [news.py](../bot/code/news.py)
   ดึงฟีดรายสัปดาห์ กรอง high impact + USD และแปลงเป็นเวลา server · [NEWS_GUARD.md](NEWS_GUARD.md)
7. **offset เวลา server เพี้ยนตอนตลาดปิด** — วัดได้ `UTC-43.5` เพราะ tick ล่าสุดเก่า 43 ชม.
   แก้ให้คืน `None` เมื่อ tick ค้าง แล้วถอยไปใช้ค่าที่จำไว้หรือค่า fallback

### รอบที่สอง (2026-07-27) — บัคที่พบตอนตรวจก่อนขึ้น live

พบ 14 จุด แก้แล้วทั้งหมด มีเทสต์ครอบใน [test_trader.py](../tests/test_trader.py) ซึ่งเดิม
`bot/code/trader.py` **ไม่มีเทสต์เลยแม้แต่ตัวเดียว** ทั้งที่เป็นไฟล์ที่ส่งออเดอร์

1. **แพ้ 3 ไม้ติด = บอทหยุดถาวร** — `roll_day` ไม่ reset `consecutive_losses` และตัวนับ
   ล้างได้ด้วยไม้กำไรเท่านั้น ซึ่งไม่มีวันเกิดเพราะบอทไม่เปิดไม้แล้ว
2. **leg ถูกปฏิเสธกลางทาง = position ลอยที่บอทไม่รู้จัก** — บันทึก trade ลง state
   ก่อนส่ง leg แรก และ adopt leg ที่ส่งสำเร็จใน `finally`
3. **ไม้ที่เข้าด้วย limit ไม่มี timeout 120 แท่ง** — `sync_fills` ไม่เคยเซ็ต `fill_bar_time`
4. **R ไม่หัก commission/swap** — `deal.profit` เป็นผลจากราคาเท่านั้น (README เคยอ้างผิด)
5. **pause แล้ว force-close ไม้ที่กำไรอยู่** — flatten เฉพาะ fatal halt
6. **ปฏิทินข่าวว่างเปล่านับเป็นใช้ได้** — blackout หายเงียบ ๆ
7. **daily loss อ้าง balance ต้นวันเท่านั้น** — FTMO ใช้ค่าที่สูงกว่าระหว่าง balance/equity
8. **risk คิดจาก balance ปัจจุบัน** — ทำให้ risk ไล่ตามกำไร เกินเพดาน 0.45% ที่คำนวณไว้
9. `count_trading_day` ไม่นับ limit fill · 10. `seen_plan_ids` เขียนแต่ไม่มีใครอ่าน ·
11. `stops_level_points` อ่านแต่ไม่เคยตรวจ · 12. `round(volume, 2)` ไม่ตาม `volume_step` ·
13. `closed_deals` ผสมเวลา server กับเวลาเครื่อง · 14. `Settings.dry_run` เป็นสวิตช์หลอก
   (ลบออก และ unknown key ใน `settings.local.json` จะ error ทันที)

**เงื่อนไขที่พบว่ายังไม่มีใครบังคับ และเพิ่มแล้ว:**

- **ไม่หยุดเมื่อสอบผ่าน** — `objectives_met` ถูกคำนวณแต่ไม่มีใครใช้ → เพิ่ม `stop_at_target`
- **โควตา 2,000 requests/วัน ไม่เคยถูกนับ** — `max_requests_per_day` เป็นค่าที่ตั้งไว้เฉย ๆ
  → นับจริงทุก call และหยุดเปิดไม้ที่ 90%
- **ไม่เคยเช็ค margin** — `margin_free` ถูกอ่านแต่ไม่ถูกใช้ → `margin_available`

### สิ่งที่ยังต้องทำมือ

- **วันหยุด/ปิดเร็ว** — MT5 5.0.5735 **ไม่มี** `symbol_info_sessions_quote/trade`
  (เช็คแล้ว) จึงดึงตารางจาก API ไม่ได้ ต้องกรอก `market_closures` เองจากประกาศโบรก
  เช่น `["2026-12-24 20:00", "2026-12-28 01:00", "Christmas"]` หรือ `"2026-12-25"`
  ถ้าลืมกรอก ด่านจะเหลือแค่ weekly close — `--status` โชว์ว่าลิสต์ว่าง
- **เวลาปิดตลาดจริงของโบรกเกอร์** — `weekly_close_hour` ตั้งไว้ 23:00 และ `weekly_open_hour` 01:00
  `--status` วัดเวลาแท่งสุดท้ายของวันศุกร์จากข้อมูลจริงมาเทียบให้ และเตือน
  `<-- CHECK weekly_close_hour` ถ้าคลาดกันเกิน 1 ชม.
- **`fallback_server_utc_offset = 3.0`** ตั้งไว้สำหรับ Exness (EET/EEST)
  → เปลี่ยนเป็น `2.0` เมื่อย้ายไป FTMO (CE(S)T) หรือปล่อยให้บอทวัดเองวันตลาดเปิด
- **`news_require_calendar = False`** ถูกต้องตอนสอบ (FTMO ไม่จำกัดข่าวตอน Challenge)
  → ตั้ง `True` ทันทีที่ได้บัญชี funded แบบ Standard
- **ค่า objective ทุกตัวเป็นค่าที่ hardcode ไว้** ไม่ได้ดึงจาก FTMO dashboard
- **ข้อมูล backtest มาจาก Exness (server GMT+2/+3 = EET/EEST)** ซึ่ง **เร็วกว่า CE(S)T 1 ชั่วโมง**
  → ขอบวันในผล backtest กับขอบวันที่ FTMO ใช้คิด daily loss เหลื่อมกัน 1 ชม.
  ผลกระทบเล็กเพราะ daily loss ไม่ใช่ข้อจำกัดที่บีบเราอยู่แล้ว (วันแย่สุดในจำลอง −2.5% เทียบลิมิต 5%)
  แต่ต้องรู้ไว้

---

## แหล่งอ้างอิง

- [Trading Objectives](https://ftmo.com/en/trading-objectives/) — objective ทุกข้อของทั้งสองสินค้า
- [Forbidden Trading Practices](https://ftmo.com/en/forbidden-trading-practices/) — ข้อห้ามและบทลงโทษ
- [Which instruments can I trade and what strategies am I allowed to use?](https://ftmo.com/en/faq/which-instruments-can-i-trade-and-what-strategies-am-i-allowed-to-use/) — EA อนุญาต, ลิมิต 200 orders / 2000 positions
- [Can I trade news?](https://ftmo.com/en/faq/can-i-trade-news/) — ±2 นาที เฉพาะบัญชี funded Standard
- [FTMO Challenge: 2-Step](https://ftmo.com/en/2-step-challenge/) และ [1-Step](https://ftmo.com/en/1-step-challenge/)
- [Trade without any time limit](https://ftmo.com/en/blog/trade-without-any-time-limit-and-take-as-long-as-you-want-to-pass/) — ยกเลิกลิมิตเวลา
- [Do I have to close my positions overnight?](https://ftmo.com/en/faq/do-i-have-to-close-my-positions-overnight/) · [FTMO Swing account type](https://ftmo.com/en/faq/ftmo-swing-account-type/)
- [Account specifications](https://ftmo.com/en/faq/what-are-the-account-specifications/) — leverage
