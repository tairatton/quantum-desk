# ระบบกันเปิดออเดอร์ตอนข่าว

สร้างเมื่อ 2026-07-26 · โค้ดอยู่ที่ [bot/code/news.py](../bot/code/news.py) และ
[bot/code/guardrails.py](../bot/code/guardrails.py) · เทสต์ 59 ตัวผ่านทั้งหมด

---

## สรุปสถานะ

| หัวข้อ | ก่อนหน้า | ตอนนี้ |
|---|---|---|
| แหล่งปฏิทินข่าว | ไม่มี — ต้องพิมพ์เวลาข่าวเองใน `news_times` (ค่าเริ่มต้นว่าง) | ดึงจากฟีด JSON รายสัปดาห์อัตโนมัติ + cache ลงดิสก์ |
| กรองความสำคัญ | ไม่มี | High impact เท่านั้น (ปรับได้ถึง medium/low) |
| กรองสกุลเงิน | ไม่มี | USD เท่านั้น (ทองเป็นสินทรัพย์อิง USD) |
| แปลงเขตเวลา | ไม่มี | แปลง UTC → เวลา server ของโบรกเกอร์อัตโนมัติ |
| ตอนโหลดปฏิทินไม่ได้ | – | เลือกได้ว่าจะเทรดต่อหรือหยุด (`news_require_calendar`) |
| แสดงในสถานะ | ไม่มี | `--status` บอกจำนวนอีเวนต์ อายุ cache และข่าวถัดไป |

**ทำไมต้องมี:** MetaTrader5 build 5.0.5735 ที่ติดตั้งอยู่ **ไม่มี calendar API**
(`dir(mt5)` ไม่พบฟังก์ชัน `calendar_*` เลย) จึงต้องดึงปฏิทินจากภายนอกเอง

---

## กฎที่ระบบนี้รองรับ

| กฎ FTMO | ใช้ตอนไหน | ระบบทำอะไร |
|---|---|---|
| บัญชี funded **Standard** ห้ามเปิด/ปิด/ตั้ง pending ในช่วง **±2 นาที** รอบข่าวที่กำหนด | หลังผ่านเป็น funded | บล็อกการเปิดไม้ในหน้าต่าง ±2 นาที |
| **Gap trading** — ห้ามเปิดไม้รอบข่าวใหญ่ | **ทุกเฟส รวมตอนสอบ** | เดียวกัน |
| ตอน Challenge / Verification ไม่จำกัดการเทรดข่าว | ตอนสอบ | `news_require_calendar=False` → โหลดปฏิทินไม่ได้ก็ยังเทรดต่อ |

> **หน้าต่างเริ่มต้นคือ ±2 นาที ตามที่ FTMO กำหนดเท่านั้น ไม่กว้างกว่านั้น**
> เพราะ backtest เทรดผ่านข่าวทั้งหมด การขยายหน้าต่างเป็นการตัดสินใจเชิงกลยุทธ์
> ไม่ใช่กฎ และจะทำให้ผลจริงเบนออกจาก edge ที่วัดไว้โดยเจตนา

---

## การตั้งค่า

| ค่า | ค่าเริ่มต้น | ความหมาย |
|---|---|---|
| `news_enabled` | `True` | ปิดเป็น `False` แล้วจะใช้แค่ `news_times` ที่พิมพ์เอง |
| `news_source_url` | ฟีด JSON รายสัปดาห์ | เปลี่ยนได้ถ้ามีแหล่งอื่น |
| `news_currencies` | `("USD",)` | ทองอิง USD · ใส่หลายสกุลได้ |
| `news_min_impact` | `"high"` | `low` / `medium` / `high` |
| `news_minutes_before` | `2` | นาทีก่อนข่าวที่ห้ามเปิด |
| `news_minutes_after` | `2` | นาทีหลังข่าวที่ห้ามเปิด |
| `news_cache_hours` | `6.0` | อายุ cache ก่อนดึงใหม่ |
| `news_require_calendar` | `False` | **ตั้ง `True` ทันทีที่ได้บัญชี funded แบบ Standard** |
| `news_times` | `()` | ข่าวเพิ่มเองแบบ `"2026-08-01 14:30"` เป็น **เวลา server** |
| `fallback_server_utc_offset` | `3.0` | ใช้เฉพาะช่วงที่ยังวัด offset จริงไม่ได้ |

ทับค่าได้ด้วย `bot/code/settings.local.json` หรือ env `BOT_NEWS_MINUTES_BEFORE=5` เป็นต้น

## ลำดับการทำงาน

1. **โหลดปฏิทิน** — ถ้า cache อายุน้อยกว่า `news_cache_hours` ใช้ cache ทันที
   ถ้าเก่าแล้วจึงดึงจากเน็ต ถ้าดึงไม่สำเร็จก็ถอยไปใช้ cache เก่าพร้อมเตือน
   ทุกกรณีไม่มีการ raise — ตลาดต้องเทรดต่อได้แม้ฟีดล่ม
2. **กรอง** เอาเฉพาะ impact ≥ high และสกุลเงินใน `news_currencies`
3. **แปลงเวลา** ฟีดเป็น UTC แต่ทุก timestamp จาก MT5 เป็นเวลา server →
   เลื่อนด้วย offset ที่วัดได้ · รายการที่พิมพ์เองใน `news_times` ถือเป็นเวลา server อยู่แล้ว
4. **ตรวจก่อนเปิดไม้** `entry_window_open` ปฏิเสธถ้าเวลาปัจจุบันอยู่ในหน้าต่างใดหน้าต่างหนึ่ง
   และบันทึกเหตุผลลง `journal.jsonl` เป็นอีเวนต์ `entry_blocked`

`entry_window_open` รับหน้าต่างข่าวเป็นพารามิเตอร์ ไม่ได้เรียกเน็ตเอง —
guardrails จึงยัง pure และเทสต์ได้โดยไม่ต้องต่อเน็ต

## การหา offset ของเวลา server — และบั๊กที่เจอ

ฟีดข่าวเป็น UTC แต่ MT5 รายงานทุกอย่างเป็นเวลา server และ **MT5 ไม่มี API บอก offset**
วิธีมาตรฐานคือลบเวลา server ด้วย UTC จริง

**บั๊กที่เจอจากการรันจริง:** รันวันอาทิตย์ได้ผลลัพธ์ `server UTC-43.5` เพราะ tick ล่าสุด
เป็นของวันศุกร์ 20:55 ซึ่งเก่า 43 ชั่วโมง → การลบให้ค่าขยะ และเวลาข่าวทั้งหมดจะเพี้ยนตาม

**วิธีแก้:** `Broker.server_utc_offset()` คืน `None` ถ้า tick เก่าเกินไป
(เกินช่วง −12.5 ถึง +14.5 ชม. หรือห่างจากค่าปัดครึ่งชั่วโมงเกิน 10 นาที) แล้ว `resolve_offset`
เลือกตามลำดับ:

1. **measured** — วัดจาก tick สด (ตลาดเปิด) แล้วจำลง `state.json`
2. **remembered** — ค่าที่วัดได้ครั้งล่าสุด (ใช้ตอนตลาดปิด)
3. **fallback** — `fallback_server_utc_offset` สำหรับการรันครั้งแรกที่ตรงวันหยุด

`--status` บอกด้วยว่ากำลังใช้แบบไหน และพิมพ์ `[CLOCK] broker server runs UTC+3`
ตอนเรียนรู้ค่าใหม่

หมายเหตุเขตเวลา: **Exness = EET/EEST (UTC+2/+3)** ส่วน **FTMO = CE(S)T (UTC+1/+2)**
ต่างกัน 1 ชั่วโมง ค่า fallback ตั้งไว้ 3.0 สำหรับ Exness — **เปลี่ยนเป็น 2.0 เมื่อย้ายไป FTMO**
(หรือปล่อยให้บอทวัดเองในวันที่ตลาดเปิด)

## ตัวอย่างผลรันจริง

```text
news      source cache  5 relevant events  age 0.0h  blackout ±2/2 min  server UTC+3 (fallback)
  next                     Wed 29 Jul 21:00 server · USD Federal Funds Rate
```

FOMC ประกาศ 18:00 UTC → 21:00 บน server UTC+3 ตรงตามที่ควรเป็น

## เทสต์ที่ครอบไว้

| เทสต์ | ตรวจอะไร |
|---|---|
| `test_feed_rows_become_utc_events` | แปลงฟีดเป็น event UTC ถูกต้อง |
| `test_only_high_impact_usd_events_are_relevant_by_default` | กรองเหลือ NFP ตัวเดียวจาก 3 อีเวนต์ |
| `test_widening_impact_and_currencies_picks_up_more` | ขยายเป็น medium + EUR ได้ครบ 3 |
| `test_window_is_shifted_onto_the_server_clock` | ข่าว 12:30 UTC → หน้าต่าง 15:28–15:32 บน server UTC+3 |
| `test_manual_entries_are_treated_as_server_time_already` | รายการพิมพ์เองไม่ถูกเลื่อนซ้ำ |
| `test_broken_rows_are_skipped_not_fatal` | แถวไม่มีวันที่/วันที่พัง ข้ามไป ไม่ล้ม |
| `test_next_event_looks_forward_only` | ข่าวถัดไปมองไปข้างหน้าเท่านั้น |
| `test_news_blackout_blocks_only_its_own_window` | บล็อกในหน้าต่าง ปล่อยผ่านนอกหน้าต่าง |
| `test_missing_calendar_blocks_entries_only_when_asked_to` | fail-open/fail-closed ตาม `news_require_calendar` |
| `test_live_tick_is_measured_and_remembered` | วัด offset แล้วจำ |
| `test_stale_tick_reuses_the_remembered_offset` | tick ค้าง → ใช้ค่าที่จำไว้ |
| `test_first_run_over_a_weekend_uses_the_configured_fallback` | รันครั้งแรกวันหยุด → ใช้ค่า fallback |

## ข้อจำกัดที่ยังเหลือ

- ฟีดเป็น **รายสัปดาห์** ถ้าบอทรันข้ามสัปดาห์โดยไม่ดึงใหม่ (cache 6 ชม. ปกติจะดึงเอง)
  อีเวนต์จะหมดอายุ — ดูค่า `age` ใน `--status` เป็นระยะ
- ไม่มีการยืนยันจาก FTMO ว่า "selected news announcements" ของเขาคือรายการเดียวกับ
  high impact ในฟีดนี้ ถ้าเข้มงวดให้ขยายเป็น `news_min_impact="medium"`
- บล็อกแค่ **การเปิดไม้ใหม่** ไม่ได้บล็อกการปิด — เพราะ SL/TP ฝากไว้ที่โบรกเกอร์
  ซึ่งอาจทำงานในหน้าต่างข่าวได้ กฎ FTMO ห้าม "open or close" แต่การถูก stop out
  ไม่ใช่การกระทำของเรา ประเด็นนี้เกิดเฉพาะบัญชี funded แบบ Standard
  ถ้าจะเคร่งจริงต้องไม่ถือไม้คาบเกี่ยวเวลาข่าวเลย ซึ่งกินเข้าไปในกลยุทธ์มาก
- ยังไม่รองรับปฏิทินแบบ offline ที่ผู้ใช้ดูแลเอง นอกจาก `news_times`
