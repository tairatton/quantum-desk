"""เทอร์มินัลของบอทฟิวเจอร์ส — `python -m entrypoints.main`

หน้าจอเดียวที่ตอบสามคำถามก่อนตัดสินใจอะไรก็ตาม: ตอนนี้บัญชีเหลือระยะเท่าไรก่อนตก,
ตลาดเปิดให้เข้าไม้ไหม, และไม้ถัดไปจะสั่งกี่สัญญา

ตั้งใจให้อ่านออกโดยไม่ต้องต่อ ProjectX — ทุกอย่างที่ไม่ต้องใช้เน็ตจะแสดงเสมอ ส่วน
ยอดเงินจริงจะดึงมาก็ต่อเมื่อมี key เท่านั้น เพราะหน้าจอที่ล่มตอนเน็ตหลุดคือหน้าจอที่
ใช้ไม่ได้ตอนที่ต้องใช้ที่สุด

    python -m entrypoints.main            เมนู
    python -m entrypoints.main --status   พิมพ์สถานะครั้งเดียวแล้วออก (ใช้กับ cron ได้)
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ and __package__ != "entrypoints":
    raise SystemExit(
        "Run this from inside core/, not from the tree root above it:\n"
        "    cd bot/future/core && python -m entrypoints.main\n"
        "or just double-click bot/future/main.bat / run python bot/future/main.py"
    )
if not __package__:
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

from engine.state import BotState

from bot import guardrails, trader
from bot.broker import Broker, ProjectXError, size_contracts
from bot.settings import JOURNAL_PATH, KILL_SWITCH, STATE_PATH, Settings, load

WIDTH = 78
LINE = "=" * WIDTH


class _Ansi:
    RESET, BOLD = "\033[0m", "\033[1m"
    RED, GREEN, YELLOW, CYAN, MAGENTA, GREY = (
        "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[35m", "\033[90m")


def paint(text: str, *codes: str) -> str:
    if not sys.stdout.isatty():          # piped to a file or a log: stay plain
        return text
    return "".join(codes) + text + _Ansi.RESET


def tag(name: str, message: str, level: str = "info") -> str:
    colours = {"ok": _Ansi.GREEN, "warn": _Ansi.YELLOW, "error": _Ansi.RED,
               "live": _Ansi.MAGENTA, "info": _Ansi.CYAN}
    return f"{paint(f'[{name}]', _Ansi.BOLD, colours.get(level, _Ansi.CYAN))} {message}"


def bar(used: float, limit: float, width: int = 28) -> str:
    """How much of a limit is spent. The point is the shape, not the number.

    A number tells you $1,149 of $2,000. A bar tells you at a glance that the
    account is over halfway to the floor, which is the thing worth noticing on
    a screen you are reading quickly.
    """
    if limit <= 0:
        return " " * width
    share = max(0.0, min(1.0, used / limit))
    filled = int(round(share * width))
    colour = (_Ansi.GREEN if share < 0.5 else
              _Ansi.YELLOW if share < 0.8 else _Ansi.RED)
    return paint("#" * filled, colour) + paint("." * (width - filled), _Ansi.GREY)


def _row(label: str, value: str, note: str = "") -> str:
    return f"  {label:<22}{value:<30}{paint(note, _Ansi.GREY)}"


def account_panel(settings: Settings, state: BotState,
                  balance: float | None) -> list[str]:
    """Where the account stands against the two limits that end it."""
    equity = balance if balance is not None else float(
        state.balance_high_water or state.initial_balance or settings.account_size)
    initial = float(state.initial_balance or settings.initial_balance
                    or settings.account_size)
    floor = guardrails.max_loss_floor(settings, state, equity)
    room = equity - floor
    day_floor = guardrails.daily_loss_floor(settings, state)
    internal_floor = guardrails.internal_daily_floor(settings, state)
    standing = guardrails.progress(settings, state, equity)

    source = "live" if balance is not None else "state file (no connection)"
    lines = [
        _row("Balance", f"${equity:,.2f}", source),
        _row("Started at", f"${initial:,.2f}",
             f"high water ${max(float(state.eod_balance_high_water or 0), initial):,.2f} EOD"),
        _row("Max loss floor", f"${floor:,.2f}",
             "trailing end-of-day" if settings.trailing_max_loss else "static"),
        _row("Room to the floor", f"${room:,.2f}",
             f"{bar(settings.max_loss_limit_dollars - room, settings.max_loss_limit_dollars)}"),
        _row("Tradeable room", f"${max(0.0, room - settings.loss_room_reserve_dollars):,.2f}",
             f"${settings.loss_room_reserve_dollars:,.0f} reserved, never risked"),
    ]
    if day_floor:
        spent = max(0.0, float(state.day_start_balance) - equity)
        lines.append(_row("Today's loss", f"${spent:,.2f} of "
                                          f"${settings.daily_loss_limit_dollars:,.0f}",
                          bar(spent, settings.daily_loss_limit_dollars)))
        lines.append(_row("Internal stop", f"${internal_floor:,.2f}",
                          f"bot stands down at -${settings.internal_daily_stop_dollars:,.0f}"))
    lines.append(_row("Target", f"${standing['gain_dollars']:+,.2f} of "
                                f"${standing['target_dollars']:,.0f}",
                      bar(max(0.0, standing['gain_dollars']), standing['target_dollars'])))
    return lines


def order_panel(settings: Settings, state: BotState, equity: float) -> list[str]:
    """What the next order would be, in contracts, at three plausible stops.

    Contracts are the whole reason this venue needs its own screen: the answer
    to "how big is the next trade" is not a setting, it is a division that
    rounds down and can round down to nothing.
    """
    risk = trader.risk_for(settings, state, equity)
    lines = [_row("Risk per trade", f"${risk:,.0f}",
                  "drawdown ladder" if settings.dynamic_risk_enabled else "flat")]
    for stop_points in (5.0, 10.0, 20.0, 40.0):
        contracts = size_contracts(settings, risk, stop_points)
        per_contract = stop_points * settings.value_per_point
        if contracts:
            exit_mode = settings.resolved_exit_mode(contracts)
            legs = ("1 leg to TP3" if exit_mode == "fixed_tp3"
                    else "/".join(str(leg) for leg in
                                  trader.split_contracts(
                                      contracts, settings.leg_weights_for(contracts))))
            note = f"${contracts * per_contract:,.0f} real risk · {legs}"
            value = f"{contracts} contract{'s' if contracts > 1 else ''}"
        else:
            note = f"${per_contract:,.0f} per contract — over the ${risk:,.0f} limit"
            value = paint("no trade", _Ansi.YELLOW)
        lines.append(_row(f"  stop {stop_points:g} pts", value, note))
    return lines


def status(settings: Settings, connect: bool = True) -> int:
    state = BotState.load(STATE_PATH) if STATE_PATH.exists() else BotState()
    balance, connection = None, "not attempted"
    if connect:
        try:
            broker = Broker(settings, dry_run=True)
            broker.connect()
            balance = broker.balance()
            connection = f"account {broker.account_id} · {broker.contract_id}"
        except ProjectXError as error:
            connection = paint(str(error), _Ansi.YELLOW)

    now = datetime.now(timezone.utc)
    session = guardrails.session_open(settings, now)
    equity = balance if balance is not None else float(
        state.balance_high_water or state.initial_balance or settings.account_size)
    health = guardrails.account_health(settings, state, equity, equity)

    print(f"\n{LINE}")
    print(f"  {paint('FUTURES', _Ansi.BOLD, _Ansi.MAGENTA)} · TopStep · "
          f"{settings.contract_symbol} · {', '.join(settings.timeframes)}")
    print(f"  {paint(guardrails.exchange_now(settings, now).strftime('%a %Y-%m-%d %H:%M %Z'), _Ansi.GREY)}"
          f"  {paint('exchange clock', _Ansi.GREY)}")
    print(LINE)
    print(tag("CONNECTION", connection, "ok" if balance is not None else "warn"))
    print(tag("SESSION", session.reason, "ok" if session else "warn"))
    print(tag("HEALTH", health.reason,
              "ok" if health else "error" if health.fatal else "warn"))
    if KILL_SWITCH.exists():
        print(tag("KILL SWITCH", f"{KILL_SWITCH.name} present — no new entries", "warn"))
    print(tag("COMMISSIONED", "no — --live is refused until the checklist is done", "warn"))
    print()
    for line in account_panel(settings, state, balance):
        print(line)
    print()
    for line in order_panel(settings, state, equity):
        print(line)
    print(f"{LINE}\n")
    return 0 if health or not health.fatal else 1


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


def _toggle_stop() -> None:
    if KILL_SWITCH.exists():
        KILL_SWITCH.unlink()
        print(f"\n{LINE}\n  กลับมาเทรดแล้ว\n{LINE}")
        return
    KILL_SWITCH.parent.mkdir(parents=True, exist_ok=True)
    KILL_SWITCH.touch()
    print(f"\n{LINE}\n  หยุดแล้ว จะไม่เปิดไม้ใหม่\n"
          f"  ไม้ที่ถืออยู่ยังมี SL/TP ฝากไว้ที่ฝั่งโบรกเกอร์\n{LINE}")


def _journal() -> None:
    from engine import journal

    stats = journal.summarise(JOURNAL_PATH)
    print(f"\n{LINE}\n  สถิติจากไม้ที่ปิดแล้วจริง\n{LINE}")
    if not stats.get("trades"):
        print("  ยังไม่มีไม้ที่ปิดแล้วในสมุด")
    else:
        for key, value in stats.items():
            print(_row(str(key), str(value)))
    print(LINE)


MENU = (
    ("1", "สถานะ (ต่อ ProjectX)", lambda s: status(s, connect=True)),
    ("2", "สถานะ (ออฟไลน์ ไม่ต่อเน็ต)", lambda s: status(s, connect=False)),
    ("3", "ทดสอบการเชื่อมต่อ อ่านอย่างเดียว", None),
    ("4", "สมุดบันทึกการเทรด", None),
    ("5", "สวิตช์หยุด เปิด/ปิด", None),
    ("q", "ออก", None),
)


def menu() -> int:
    settings = load()
    while True:
        print(f"\n{LINE}\n  {paint('FUTURES TERMINAL', _Ansi.BOLD)} · "
              f"{settings.contract_symbol} · "
              f"{'หยุดอยู่' if KILL_SWITCH.exists() else 'พร้อม'}\n{LINE}")
        for key, label, _ in MENU:
            print(f"   {key}) {label}")
        try:
            choice = input("\nเลือก: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice == "q":
            return 0
        if choice == "1":
            status(settings, connect=True)
        elif choice == "2":
            status(settings, connect=False)
        elif choice == "3":
            from entrypoints.live import check
            check(settings)
        elif choice == "4":
            _journal()
        elif choice == "5":
            _toggle_stop()
        else:
            print("  ไม่มีตัวเลือกนี้")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true",
                        help="พิมพ์สถานะครั้งเดียวแล้วออก")
    parser.add_argument("--offline", action="store_true",
                        help="ไม่ต้องต่อ ProjectX")
    args = parser.parse_args(argv)
    settings = load()
    if args.status:
        return status(settings, connect=not args.offline)
    return menu()


if __name__ == "__main__":
    raise SystemExit(main())
