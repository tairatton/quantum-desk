# Forex entry points

โฟลเดอร์นี้เก็บเฉพาะจุดเริ่มโปรแกรมของ Forex:

- `main.py` — เมนู Forex
- `live.py` — launcher สำหรับ Live trading
- `main.bat` — ดับเบิลคลิกเพื่อเรียก `live.py`
- `research.py` — CLI วิเคราะห์และ backtest

โค้ด execution อยู่ใน `../bot/` และไม่ควรย้าย runtime state ระหว่างที่ Live process ทำงานอยู่
