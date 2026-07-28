"""ซ่อม risk_cash ของไม้ที่ยังเปิดอยู่ ให้ตรงกับความเสี่ยงจริงของ lot ที่ส่งไป

    python -m bot.code.repair_risk_cash            # ดูว่าจะแก้อะไร ไม่เขียนไฟล์
    python -m bot.code.repair_risk_cash --apply    # เขียนจริง

ไม้ที่เปิดก่อน 28 ก.ค. 2026 บันทึก `risk_cash` เป็น *งบที่ตั้งใจ* ไม่ใช่ความเสี่ยงจริง
ของ lot ที่ส่งได้หลังปัดลงตาม step ทำให้ `reconcile_closed` คิด R ผิด — ไม้ที่โดน SL
เต็มอ่านได้ −0.52R แทน −1.00R และ expectancy ใน journal สูงเกินจริงเกือบเท่าตัว

ต้องปิดบอทก่อนรัน ไม่งั้น process ที่ยังทำงานจะเขียน state.json ทับทันทีที่ครบรอบ
"""
from __future__ import annotations

import json
import sys

from .broker import Broker
from .settings import STATE_PATH, load
from .state import BotState


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    apply = "--apply" in argv

    config = load()
    state = BotState.load(STATE_PATH)
    open_trades = [t for t in state.trades.values() if not t.closed]
    if not open_trades:
        print("ไม่มีไม้ที่เปิดอยู่ ไม่ต้องซ่อม")
        return 0

    # value_per_point ต้องมาจากโบรกเกอร์ ไม่ควรฝังตัวเลขไว้ในโค้ด
    with Broker(config.symbol, config.magic, config.deviation_points,
                dry_run=True) as broker:
        per_point = broker.spec.value_per_point
        print(f"{broker.spec.name}  value/point {per_point:,.2f}\n")

    changed = 0
    for trade in open_trades:
        lots = round(sum(trade.legs), 8)
        actual = round(trade.risk * per_point * lots, 2)
        gap = abs(actual - trade.risk_cash)
        mark = "แก้" if gap > 0.01 else "ตรงแล้ว"
        print(f"{trade.plan_id}")
        print(f"  legs {trade.legs} = {lots:g} lot   SL ระยะ {trade.risk:.2f}")
        print(f"  risk_cash เดิม {trade.risk_cash:.2f} -> ควรเป็น {actual:.2f}   [{mark}]")
        if gap > 0.01:
            changed += 1
            if apply:
                trade.risk_cash = actual

    if not changed:
        print("\nทุกไม้ตรงแล้ว")
        return 0
    if not apply:
        print(f"\n{changed} ไม้ต้องแก้ · รันซ้ำด้วย --apply เพื่อเขียนจริง")
        return 0

    state.save(STATE_PATH)
    print(f"\nแก้ {changed} ไม้ และบันทึกลง {STATE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
