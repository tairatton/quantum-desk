# Futures entry points

โฟลเดอร์นี้เก็บเฉพาะจุดเริ่มโปรแกรมของ Futures:

- `main.py` — terminal menu และ offline status (เรียกผ่าน `bot/future/main.py`/`main.bat` ที่ root ของต้นไม้)
- `live.py` — ProjectX connection check/dry-run entrypoint

`main.bat` ย้ายออกไปอยู่ที่ root ของต้นไม้ (`bot/future/main.bat`) แล้ว ไม่ได้อยู่ในโฟลเดอร์นี้ —
จะได้เห็นแค่ `main.py`/`main.bat` ตอนเปิดโฟลเดอร์ `bot/future/` โดยไม่ต้องลงมาถึงตรงนี้

โค้ด broker/risk/trader อยู่ใน `../bot/` และยังไม่ commissioned สำหรับส่งออเดอร์จริง
