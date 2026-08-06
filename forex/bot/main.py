"""เมนูเดียวสำหรับสั่งบอท — `python -m bot.main`

ทุกอย่างที่ `bot.run` ทำได้ อยู่ในเมนูนี้ทั้งหมด ต่างกันแค่ตรงนี้ถามว่าจะทำอะไร
แทนที่จะต้องจำ flag

สองคำสั่งที่ส่งออเดอร์จริง — รันจริง และ ปิดทุกอย่าง — ต้องพิมพ์คำยืนยันเป็นตัวพิมพ์
ใหญ่ ไม่ใช่กด y เพราะการกดพลาดปุ่มเดียวไม่ควรทำให้เงินออกจากบัญชี

`bot.run` ยังใช้ได้เหมือนเดิมสำหรับคนที่ชอบ command line:

    python -m bot.run --status
    python -m bot.run --live
"""
from __future__ import annotations

import sys

from strategy.mt5_source import MT5Error

from . import run
from .settings import JOURNAL_PATH, KILL_SWITCH, load

LINE = "=" * 62


def _stopped() -> bool:
    return KILL_SWITCH.exists()


def _toggle_stop() -> None:
    """สลับสวิตช์หยุด — ไม่ต้องต่อ MT5 เลยเพราะเป็นแค่ไฟล์"""
    if _stopped():
        KILL_SWITCH.unlink()
        print(f"\n{LINE}\n  กลับมาเทรดแล้ว บอทจะเปิดไม้ใหม่ได้ตั้งแต่แท่งถัดไป\n{LINE}")
        return
    KILL_SWITCH.parent.mkdir(parents=True, exist_ok=True)
    KILL_SWITCH.touch()
    print(f"\n{LINE}\n  หยุดแล้ว จะไม่เปิดไม้ใหม่\n"
          f"  ไม้ที่ถืออยู่ยังถูกดูแลต่อ — เลื่อน BE และปิดตาม timeout ปกติ\n"
          f"  และทุก leg ยังมี SL/TP ฝากไว้ที่โบรกเกอร์\n{LINE}")


def _confirm(word: str, warning: str) -> bool:
    print(f"\n{LINE}\n{warning}\n{LINE}")
    try:
        answer = input(f"\nพิมพ์  {word}  เพื่อยืนยัน (อย่างอื่นคือยกเลิก): ").strip()
    except (EOFError, KeyboardInterrupt):
        return False
    if answer == word:
        return True
    print("\nยกเลิกแล้ว ไม่มีอะไรถูกส่ง")
    return False


def _journal() -> None:
    from engine import journal

    stats = journal.summarise(JOURNAL_PATH)
    print(f"\n{LINE}\n  สถิติจากไม้ที่ปิดแล้วจริง\n{LINE}")
    if stats.get("trades", 0) == 0:
        print("  ยังไม่มีไม้ที่ปิดแล้ว — ตัวเลขจะขึ้นหลังเทรดจริง")
        print(f"  บันทึกอยู่ที่ {JOURNAL_PATH}")
        return
    for key, value in stats.items():
        print(f"  {key:18s} {value}")
    print("\n  เทียบ expectancy_r กับ holdout ใน")
    print("  test/forex/outputs/backtests/technique_lab/XAUUSD/<TF>/report.json")
    print("  ห่างกันเกิน 0.05R ต่อไม้ หลังผ่าน 50 ไม้ = ต้นทุนจริงกินมากกว่าที่ประเมิน")


ACTIONS = {
    "1": ("ดูสถานะ (อ่านเท่านั้น ไม่ส่งอะไร)", "status"),
    "2": ("ซ้อมรันหนึ่งรอบ — ดูว่ามันจะทำอะไร", "once"),
    "3": ("ซ้อมรันแบบลูป — ปลอดภัย ไม่ส่งออเดอร์", "dry"),
    "4": ("รันจริง — ส่งออเดอร์เข้าโบรกเกอร์", "live"),
    "5": ("หยุด / กลับมาเทรด (สลับสวิตช์)", "stop"),
    "6": ("ปิดทุกอย่างทันที (ฉุกเฉิน)", "flatten"),
    "7": ("ดูสถิติ R ที่ทำได้จริง", "journal"),
    "0": ("ออก", "quit"),
}


def menu() -> None:
    settings = load()
    print(f"\n{LINE}")
    print(f"  HTF Quantum — ตัวรันเทรด")
    print(f"  {settings.symbol} {'+'.join(settings.timeframes)} · "
          f"risk {settings.risk_percent:.2f}%/ไม้ · เพดาน {settings.max_open_risk_percent:.2f}%")
    print(f"  สวิตช์หยุด: {'ON — ไม่เปิดไม้ใหม่' if _stopped() else 'off'}")
    print(LINE)
    for key, (label, _) in ACTIONS.items():
        print(f"  {key}. {label}")
    print(LINE)


def run_action(action: str) -> bool:
    """ทำหนึ่งคำสั่ง คืน False เมื่อควรออกจากโปรแกรม"""
    if action == "quit":
        return False
    if action == "stop":
        _toggle_stop()
        return True
    if action == "journal":
        _journal()
        return True

    if action == "live":
        if not _confirm("LIVE",
                        "  รันจริง — ออเดอร์จะถูกส่งเข้าบัญชีที่แสดงด้านล่าง\n\n"
                        "  ตรวจก่อน: บัญชีและยอดเงินถูกตัวไหม · entries เป็น OPEN ไหม ·\n"
                        "  observed week ends ตรงกับที่ตั้งไว้ไหม (ต้องไม่มีคำเตือน CHECK)\n\n"
                        "  หยุดภายหลังได้ด้วยเมนู 5 · ปิดทุกอย่างด้วยเมนู 6"):
            return True
        print("\nเริ่มแล้ว กด Ctrl+C เพื่อหยุด\n")
        run.execute(live=True)
        return True

    if action == "flatten":
        if not _confirm("CLOSE",
                        "  ปิดทุกอย่างทันที — ยกเลิกทุกออเดอร์และปิดทุก position\n"
                        "  ที่ราคาตลาด เดี๋ยวนี้\n\n"
                        "  นี่เป็นการออกจากไม้ที่ backtest จะถือต่อ ผลจึงไม่ตรงกับระบบ\n"
                        "  ที่วัดไว้ ถ้าแค่อยากหยุดเปิดไม้ใหม่ ใช้เมนู 5 แทน"):
            return True
        run.execute(live=True, flatten=True)
        return True

    if action == "status":
        run.execute(status=True)
    elif action == "once":
        run.execute(once=True)
    elif action == "dry":
        print("\nซ้อมรันแบบลูป — บรรทัดที่ขึ้น [DRY-RUN] คือสิ่งที่การรันจริงจะส่ง")
        print("กด Ctrl+C เพื่อหยุด\n")
        run.execute()
    return True


def main(argv: list[str] | None = None) -> int:
    """เมนูวนซ้ำ หรือทำคำสั่งเดียวถ้าส่งเลขมาทาง argv"""
    argv = sys.argv[1:] if argv is None else argv

    if argv:
        choice = argv[0]
        if choice not in ACTIONS:
            print(f"ไม่รู้จักคำสั่ง {choice!r} · เลือกได้: {', '.join(ACTIONS)}")
            return 2
        try:
            run_action(ACTIONS[choice][1])
        except MT5Error as error:
            print(f"\n[MT5] {error}")
            print("เปิด MetaTrader 5 และ login ไว้ก่อน แล้วเปิด")
            print("Tools > Options > Expert Advisors > Allow algorithmic trading")
            return 1
        return 0

    while True:
        menu()
        try:
            choice = input("เลือก: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice not in ACTIONS:
            print(f"\nไม่มีข้อ {choice!r} — เลือก {', '.join(ACTIONS)}")
            continue
        try:
            if not run_action(ACTIONS[choice][1]):
                return 0
        except MT5Error as error:
            print(f"\n[MT5] {error}")
            print("เปิด MetaTrader 5 และ login ไว้ก่อน แล้วเปิด")
            print("Tools > Options > Expert Advisors > Allow algorithmic trading")
        except KeyboardInterrupt:
            print("\n[หยุด] ไม้ที่เปิดอยู่ยังมี SL/TP ที่โบรกเกอร์ตามปกติ")


if __name__ == "__main__":
    raise SystemExit(main())
