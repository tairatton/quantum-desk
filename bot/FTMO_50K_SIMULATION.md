# Quantum Desk — FTMO 2-Step $50K Simulation

วันที่จำลอง: 28 กรกฎาคม 2026

Production checklist, กลยุทธ์เต็ม และคำอธิบาย probability ล่าสุด:
[PRODUCTION_READINESS.md](PRODUCTION_READINESS.md)

## สมมติฐาน

- Product: FTMO Challenge 2-Step, Swing
- Initial simulated capital: $50,000
- Symbol: XAUUSD
- Timeframes: M15 + M30
- Risk target: 0.40% หรือ $200 ต่อ setup
- Max open risk: 0.80% หรือ $400
- Exit: `be_33_33_34` เพราะ initial balance ตั้งแต่ $30,000
- TP1 33% ที่ +1R, TP2 33% ที่ +1.5R, TP3 34% ที่ +2R
- เลื่อน TP2/TP3 ไป Break-even หลังตรวจพบว่า TP1 ปิด
- 20,000 Monte Carlo paths, สูงสุด 400 market days ต่อ phase
- Cross-stream correlation สมมติ 0.30

คำสั่งที่บังคับ exit ให้ตรงกับ capital tier $50K:

```powershell
python scripts\ftmo_portfolio_sim.py --book "XAU M15 + M30" --risk 0.40 `
  --technique be_after_tp1_33_33_34 --nsim 20000
```

## เกณฑ์ FTMO 2-Step สำหรับ $50K

| Objective | Percent | Cash |
|---|---:|---:|
| Challenge target | +10% | +$5,000 |
| Verification target | +5% | +$2,500 |
| Maximum Daily Loss | 5% | $2,500 |
| Maximum Loss | 10% static | $5,000 |
| Minimum Trading Days | 4 วันต่อ phase | — |
| Trading period | Unlimited | — |

## ผล Backtest ของ exit ที่บัญชี $50K ใช้จริง

### XAUUSD M15 — BE33

| Split | Trades | Expectancy | Profit factor | Max DD |
|---|---:|---:|---:|---:|
| Train | 698 | +0.1446R | 1.377 | 11.347R = 4.54% |
| Validation | 207 | +0.3778R | 2.297 | 4.659R = 1.86% |
| Holdout | 221 | +0.1888R | 1.573 | 5.607R = 2.24% |

### XAUUSD M30 — BE33

| Split | Trades | Expectancy | Profit factor | Max DD |
|---|---:|---:|---:|---:|
| Train | 649 | +0.0562R | 1.126 | 24.087R = 9.63% |
| Validation | 200 | +0.2362R | 1.599 | 14.993R = 6.00% |
| Holdout | 204 | +0.3142R | 1.917 | 4.921R = 1.97% |

ช่วง holdout ที่รวม M15+M30 ตามลำดับวันจริง:

- 425 trades
- Daily correlation ประมาณ 0.237
- Net +105.81R
- Max closed-equity DD 8.323R = 3.33% ที่ risk 0.40%
- Worst closed day -3.729R = -1.49%
- Best closed day +7.485R = +2.99%

ตัวเลขนี้ไม่รวม floating intraday drawdown จึงไม่ใช่หลักฐานว่า Daily Loss ไม่มีทางถูกชน

## Monte Carlo — Edge จากแต่ละช่วง Backtest

| Regime | ผ่าน Step 1 | Breach ก่อน Step 1 | ผ่านครบ 2-Step | Step 1 median / P90 | รวมสองขั้น median / P90 |
|---|---:|---:|---:|---:|---:|
| Holdout/current | 100.00% | 0.00% | 100.00% | 38 / 57 market days | 59 / 82 market days |
| Validation/base | 100.00% | 0.00% | 100.00% | 28 / 40 | 43 / 58 |
| Train/older-flat | 99.985% | 0.015% | 99.955% | 67 / 119 | 104 / 168 |

แต่ละ regime ใช้ daily-return distribution จาก split นั้นโดยตรง ผลข้างต้นยังเป็น conditional
simulation: มันตอบว่า “ถ้าการกระจายผลและ expectancy แบบ backtest ยังใช้ได้” path risk
เป็นอย่างไร ไม่ได้คำนวณโอกาสที่ edge จะหายไปในอนาคต

## Stress test เมื่อ Edge เสื่อม

คงรูปแบบการแกว่งรายวันเดิม แต่บังคับ expectancy ใหม่ต่อ trade:

| Expectancy ที่เหลือ | ผ่าน Step 1 | Breach ก่อน Step 1 | ผ่านครบ 2-Step | รวมสองขั้น median / P90 |
|---:|---:|---:|---:|---:|
| +0.05R | 93.04% | 1.49% | 90.95% | 227 / 401 market days |
| 0.00R | 32.58% | 34.03% | 19.74% | 313 / 509 |
| -0.05R | 1.39% | 93.29% | 0.16% | 211 / 372 สำหรับ paths ส่วนน้อยที่ผ่าน |

ข้อสรุปสำคัญ: ความเสี่ยงหลักไม่ใช่ขนาดบัญชี $50K แต่คือ live expectancy หลัง spread,
commission, swap, slippage, news filter และ BE polling เหลือเท่าใด

## Lot โดยประมาณบน $50K

ค่าจริงขึ้นกับระยะ SL ของแต่ละ setup:

| Timeframe | Total lot โดยประมาณ | การแบ่ง |
|---|---:|---|
| M15 | 0.10 lot | 0.03 / 0.03 / 0.04 |
| M30 | 0.06 lot | 0.02 / 0.02 / 0.02 |

ทั้งสอง timeframe แบ่งขั้นต่ำ 0.01 lot ได้ แต่ setup ที่ SL กว้างผิดปกติยังสามารถถูกปฏิเสธได้

## สิ่งที่แบบจำลองยังไม่รวม

- Floating intraday DD และลำดับราคาในแท่ง
- Gap และ slippage รุนแรง
- BE client-side ที่ตรวจทุก 180 วินาที
- MT5/อินเทอร์เน็ตหยุดทำงาน
- Pending order ถูกยกเลิกก่อน expiry
- Signal ที่ถูกบล็อกเพราะข่าว, closure, opposing position หรือ request limit
- ความแตกต่างระหว่าง Exness backtest feed กับ FTMO feed
- การเปลี่ยน regime หลังปี 2026

## ก่อนเริ่ม Challenge $50K

ห้ามนำ `state.json` ของบัญชี $10K เดิมไปใช้ต่อ เพราะมี initial balance, trading days,
loss streak, seen plans และ trade tickets ของบัญชีเดิม บัญชี Challenge และ Verification
ต้องมี state ใหม่ของตัวเอง

Checklist:

1. ใช้ FTMO 2-Step Swing หากต้องถือข้ามคืน/สุดสัปดาห์
2. Archive state/journal ของบัญชีเดิมก่อนเปลี่ยน login
3. เริ่ม state ใหม่และตรวจว่า initial balance = 50,000
4. ตรวจ terminal ว่า active exit = `be_33_33_34`
5. Challenge ใช้ profit target 10%; Verification ต้องเปลี่ยนเป็น 5%
6. Forward-test บัญชี FTMO demo/free trial ก่อนซื้อจริง
7. ตรวจอย่างน้อย 50 live-demo trades ว่า expectancy หลังต้นทุนยังเป็นบวก

## สรุป

ภายใต้ edge ระดับ train/validation/holdout ใน backtest ระบบมีโอกาสผ่านสูงและ DD ที่ risk
0.40% อยู่ในกรอบของ FTMO 2-Step แต่ไม่สามารถการันตีการสอบผ่านได้ หาก live edge ลดเหลือ
ใกล้ศูนย์ โอกาสผ่านจะลดลงอย่างรุนแรง จึงควรใช้ Free Trial/FTMO Demo ยืนยัน execution ก่อนซื้อ
และไม่ควรเพิ่ม risk เกิน 0.40% เพื่อเร่งสอบ
