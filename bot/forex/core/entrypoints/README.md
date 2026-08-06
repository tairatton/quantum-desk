# Forex entry points

โฟลเดอร์นี้เก็บเฉพาะจุดเริ่มโปรแกรมของ Forex:

- `main.py` — เมนู Forex (เรียกผ่าน `bot/forex/main.py` ที่ root ของต้นไม้)
- `live.py` — launcher สำหรับ Live trading (เรียกผ่าน `bot/forex/main.bat` ที่ root ของต้นไม้)
- `research.py` — CLI วิเคราะห์และ backtest

`main.bat` ย้ายออกไปอยู่ที่ root ของต้นไม้ (`bot/forex/main.bat`) แล้ว ไม่ได้อยู่ในโฟลเดอร์นี้ —
จะได้เห็นแค่ `main.py`/`main.bat` ตอนเปิดโฟลเดอร์ `bot/forex/` โดยไม่ต้องลงมาถึงตรงนี้

โค้ด execution อยู่ใน `../bot/` และไม่ควรย้าย runtime state ระหว่างที่ Live process ทำงานอยู่
