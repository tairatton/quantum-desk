# โครงสร้างใหม่ของ bot/ — สรุปการย้ายและผลตรวจบั๊ก

วันที่: 2026-08-06

## สิ่งที่เปลี่ยน

ย้ายจาก `forex/` และ `future/` ที่ root ของ repo เข้าไปอยู่ใต้ `bot/` โดยแต่ละต้นไม้
เหลือแค่ `main.py` + `main.bat` ให้เห็นตอนเปิดโฟลเดอร์ ส่วนที่เหลือทั้งหมด (`bot/`,
`engine/`, `strategy/`, `entrypoints/`, `tools/`, `test/`) ย้ายไปอยู่ใต้ `core/`

```
bot/forex/main.py · main.bat · core/{bot,engine,strategy,entrypoints,tools,test}/
bot/future/main.py · main.bat · core/{bot,engine,strategy,entrypoints,tools,test}/
```

`main.py` เป็นไฟล์ใหม่บางๆ — แค่เติม `core/` เข้า `sys.path` แล้วเรียก
`core/entrypoints/main.py:main()` ตัวจริง ไม่มีตรรกะซ้ำ `main.bat` ย้ายออกมาจาก
`entrypoints/` เดิม ปรับ path การหา `.venv` ให้ลึกขึ้นอีกสองระดับ (`bot/<tree>/` แทนที่จะ
เป็น `<tree>/` ที่ root)

## ของที่ลบไปพร้อมกัน (ยืนยันแล้วว่าไม่ได้ใช้งานจริงก่อนลบ)

- โฟลเดอร์ว่าง `forex/future/`, `outputs/charts/analysis/`
- 5 ไฟล์ใน `future/strategy/` ที่ก๊อปมาจาก `forex/` แต่ไม่เคยถูก import ใน `future/` เลย:
  `ai_historical.py`, `equity_chart.py`, `quantum_chart.py`, `backtest_reporting.py`,
  `service.py`
- import/function ที่ไม่ได้ใช้: `import math` (ทั้งสอง `quantum.py`), `MT5Error` ใน
  `trader.py`, `import json` ใน `repair_risk_cash.py`, ฟังก์ชัน `flat_deadline()` และ
  `trading_day_start()` ที่ไม่มีใครเรียก
- โฟลเดอร์ `bot/code/`, `scripts/`, `tests/` ที่ root เดิม — เศษเดิมจากก่อนแยกสองต้นไม้
  (`__pycache__` + state file ที่หยุดอัปเดตตั้งแต่ก่อน `forex/bot/` เริ่มทำงาน ไม่ใช่ของที่
  บอทตัวปัจจุบันใช้)

## บั๊กที่พบระหว่างย้าย และวิธีแก้

| บั๊ก | ผลถ้าไม่แก้ | แก้ยังไง |
|---|---|---|
| `.git/info/exclude` มีบรรทัด `/bot/` ค้างจากตอนที่ใช้กันไม่ให้ git เห็นเศษ `bot/code/` เดิม | **`bot/` ใหม่ทั้งก้อนจะไม่ถูก git track เลย** — ดูเหมือนย้ายสำเร็จแต่ `git status` จะว่างเปล่า ถ้า push/commit ตอนนี้จะไม่มีอะไรถูกบันทึก | ลบบรรทัด `/bot/` ออกจาก `.git/info/exclude` (เหลือแค่ `/.codex/` ที่ไม่เกี่ยวกับซอร์ส) |
| `future/test/unit/test_packaging.py`: `REPO = TREE.parent` คำนวณ repo root ผิดหลังย้าย (`TREE` ตอนนี้คือ `core/` ซึ่งลึกกว่าเดิม 2 ชั้น) และเทสยังยิง `python -m future.entrypoints.live` ซึ่งไม่มีอยู่จริงแล้วที่ระดับนั้น | เทส `test_root_level_module_execution_explains_itself` และ `test_future_entrypoint_from_root_explains_tree_boundary` จะแดงทันที | เปลี่ยนเป็นจำลอง "รันตื้นไป 1 ชั้น" จริง (`python -m core.entrypoints.live` จาก `bot/future/`) และแก้ assertion ให้ตรงกับข้อความ guard ใหม่ |
| guard message ใน `entrypoints/live.py`, `main.py`, `research.py` (ทั้งสองต้นไม้) ยังบอกให้ `cd forex`/`cd future` ซึ่งไม่มีโฟลเดอร์นั้นที่ root แล้ว | ผู้ใช้ที่รันผิดที่จะได้คำแนะนำที่พาไปผิดทาง | แก้ข้อความเป็น `cd bot/<tree>/core && python -m entrypoints.X` พร้อมบอกทางลัดว่าดับเบิลคลิก `main.bat`/รัน `main.py` ได้เลย |
| README/docstring ที่เหลือ (`bot/forex/core/bot/README.md`, `entrypoints/README.md` ทั้งสองต้นไม้, `README.md`, `DOCUMENT.md`) อ้าง path เก่า (`forex/bot/...`, `cd forex`, `entrypoints/main.bat`) | เอกสารพาไปเปิดไฟล์ที่ไม่มีอยู่แล้ว | อัปเดต path ทุกจุดที่เจอให้ตรงกับตำแหน่งใหม่ |
| legacy launcher `tools/launch/Quantum-Bot.bat` หา `.venv` ด้วย `..\.venv` (นับจากตำแหน่งเดิม 1 ชั้นจาก root) | หลังย้าย โฟลเดอร์นี้ลึกขึ้นจาก root repo อีก 2 ชั้น ปุ่มนี้จะหา venv ไม่เจอและ fallback ไป `python` เฉยๆ (อาจไม่ใช่ interpreter ที่มี dependency ครบ) | แก้เป็น `..\..\..\.venv` ให้ตรงความลึกใหม่ |
| `.gitignore` มี path เก่า (`forex/bot/state.json` ฯลฯ) ที่ไม่ตรงกับตำแหน่งไฟล์จริงอีกต่อไป | ไฟล์ state/journal/settings.local.json ที่ตำแหน่งใหม่จะไม่ถูก ignore — เสี่ยง commit ข้อมูลบัญชีจริงเข้า repo โดยไม่ตั้งใจ | ปรับทุก path ให้ตรงกับ `bot/<tree>/core/...` |

## ผลตรวจ (หลังแก้ทั้งหมด)

| ต้นไม้ | วิธีตรวจ | ผล |
|---|---|---|
| ทั้งคู่ | `py_compile` ทุกไฟล์ `.py` ใน `bot/` | ผ่านหมด ไม่มี syntax error |
| forex | `python -m unittest discover -s test/unit` (`cd bot/forex/core`) | **302 เทส ผ่าน 299 · fail 3** (ดูหมายเหตุด้านล่าง — ไม่เกี่ยวกับการย้าย) |
| future | `python -m unittest discover -s test/unit` (`cd bot/future/core`) | **113 เทส ผ่านหมด** รวมเทสที่แก้ใหม่ 3 ตัวใน `test_packaging.py` |
| forex | `python bot/forex/main.py 1` (เมนู, action สถานะ) จาก repo root | ต่อ MT5 จริง (บัญชี FTMO-Demo) อ่านสถานะได้ครบ |
| forex | `python -m entrypoints.live` (`cd bot/forex/core`) รัน 8 วินาทีแล้วตัด | เชื่อมต่อ, sync, heartbeat ปกติ — **ไม่มีไม้ถูกเปิด** (`positions: 0` ใน journal) |
| future | `python bot/future/main.py --status --offline` จาก repo root | อ่านสถานะจาก state file ได้ครบ ไม่ต้องต่อเน็ต |
| future | `python -m core.entrypoints.live --help` จาก `bot/future/` (จำลองรันผิดที่) | ได้ guard message ใหม่ตามที่แก้ ไม่ใช่ ImportError ดิบ |

### 3 เทสที่ยังแดงในฝั่ง forex — ไม่ใช่บั๊กจากการย้าย

`test_quantum_entry.py::ConversionSwitchTests` ต้องอ่าน
`bot/forex/core/test/data/market/XAUUSD/M30.csv` ซึ่งเป็นไฟล์ market data ที่ `.gitignore`
ระบุไว้ตั้งแต่แรกว่าต้อง fetch เอง ไม่ commit เข้า repo (`# Market data ... reproducible
from the venue's own feed`) — เช็คแล้วว่าไฟล์นี้ไม่เคยมีอยู่ใน repo หรือในเครื่องนี้เลย
ไม่ใช่แค่ที่ path เก่า สรุปคือเทสนี้ fail อยู่ก่อนย้ายแล้วเหมือนกัน ต้อง fetch ข้อมูลเองถึงจะผ่าน

## จุดที่ยังค้างอยู่ ไม่ได้แตะ (นอกขอบเขตการย้าย)

- `DOCUMENT.md` ข้อ 8 ("สิ่งที่ยังไม่เสร็จ") พูดถึงไฟล์
  `forex/bot/journal.pre-split-20260806.jsonl` ซึ่งตอนนี้ไม่มีอยู่จริงแล้วในเครื่องนี้เลย
  (เข้าใจว่าถูก merge เข้า journal หลักไปแล้วตามที่ข้อความในเอกสารบอกให้ทำ) —
  ไม่ได้แก้ข้อความส่วนนี้ให้ เพราะเป็นการตัดสินใจเรื่องเนื้อหา ไม่ใช่แค่ path
- `state.pre-fix-20260731.json` ที่ข้อ 9 พูดถึง (มี conflict marker ค้าง) ยังอยู่ที่เดิม
  แค่ย้าย path เฉยๆ ไม่ได้แก้เนื้อหา
