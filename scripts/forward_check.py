"""Compare what the bot has actually done against what the backtest promised.

Every number in `bot/README.md` is a backtest number. This reads the live journal
instead and puts the two side by side, because the only question that decides
whether the system is worth running is whether the measured edge survived contact
with a real broker — a different feed, a wider spread, real slippage.

    python scripts/forward_check.py
    python scripts/forward_check.py --journal path/to/journal.jsonl

The verdict thresholds come from `docs/ENTRY_TIMING_EXPERIMENT_2026-07-31.md`:
50 closed trades before the sample means anything, +0.15R as the line where the
holdout expectancy is confirmed rather than merely not contradicted.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xau import backtest_reporting as br  # noqa: E402

#: Trades needed before the sample says anything at all. `bot/README.md` sets it.
MIN_TRADES = 50
#: Expectancy at or above which the holdout figure counts as confirmed.
CONFIRM_R = 0.15
#: Below this the edge has not appeared and no challenge should be bought.
ABANDON_R = 0.05
#: Cost per trade, as a fraction of that trade's own risk, above which costs are
#: eating enough of the edge to investigate. A rough tripwire, not a budget: the
#: holdout expectancies being compared against are +0.17R and +0.34R, so a tenth
#: of R per trade is already a third of the thinner one.
#:
#: It deliberately does *not* try to price the conversion's slippage headroom
#: ($1.22 on M15, $0.77 on M30). That budget is extra slippage on the converted
#: market order alone, and the journal records only the finished trade's total
#: round-trip cost — spread, commission and slippage across every leg, converted
#: or not. Isolating the conversion would need the fill price against the plan's
#: intended entry, which nothing writes down yet.
COST_R_TRIPWIRE = 0.10


def load(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def closed_trades(events: list[dict]) -> list[dict]:
    """Closed trades, each carrying the risk it was sized for.

    `trade_closed` reports cost in cash, which says nothing on its own — $5 is
    nothing against a $200 risk and everything against $20. The matching
    `trade_opened` holds `risk_cash`, so joining on `plan_id` is what makes cost
    comparable to the R figures the backtest is quoted in.
    """
    risk_by_plan = {}
    for event in events:
        # A converted market fill can become visible only after `trade_opened`.
        # Its later deal-derived value supersedes the conservative sizing value.
        if (event.get("event") in {"trade_opened", "risk_cash_actualized"}
                and event.get("risk_cash")):
            risk_by_plan[event["plan_id"]] = event["risk_cash"]
    trades = []
    for event in events:
        if event.get("event") != "trade_closed" or event.get("r") is None:
            continue
        trade = dict(event)
        trade["risk_cash"] = risk_by_plan.get(event.get("plan_id"))
        trades.append(trade)
    return trades


def holdout_expectancy(timeframe: str) -> float | None:
    """What the locked split promised for the exit the $50K account runs."""
    try:
        report = br.load_report(br.report_path("XAUUSD", timeframe))
    except (FileNotFoundError, OSError):
        return None
    technique = report["techniques"].get("be_after_tp1_33_33_34")
    return technique["holdout"]["expectancy_r"] if technique else None


def summarise(trades: list[dict]) -> dict:
    values = [float(trade["r"]) for trade in trades]
    wins = [value for value in values if value > 0]
    # Journal costs are negative cash; the magnitude over the trade's own risk is
    # what compares with the backtest's cost model.
    cost_r = [abs(float(trade.get("costs") or 0.0)) / float(trade["risk_cash"])
              for trade in trades if trade.get("risk_cash")]
    return {
        "n": len(values),
        "net_r": sum(values),
        "expectancy_r": statistics.fmean(values) if values else 0.0,
        "win_rate": len(wins) / len(values) * 100 if values else 0.0,
        "cost_r": statistics.fmean(cost_r) if cost_r else None,
        "priced": len(cost_r),
    }


def verdict(n: int, expectancy: float) -> tuple[str, str]:
    if n < MIN_TRADES:
        return ("ยังตัดสินไม่ได้",
                f"มี {n} ไม้ จาก {MIN_TRADES} ที่ต้องการ — ตัวเลขยังไม่มีน้ำหนัก")
    if expectancy >= CONFIRM_R:
        return ("edge ยืนยันแล้ว",
                "execution รักษา edge ได้ · คง risk 0.40% ตามแบบจำลองที่อนุมัติไว้")
    if expectancy >= ABANDON_R:
        return ("edge อ่อนกว่าคาดแต่ยังมี", "คง risk 0.40% ไว้ อย่าเร่ง")
    return ("edge ไม่ปรากฏ",
            "อย่าซื้อ challenge — กลับไปตั้งคำถามกับกลยุทธ์ ไม่ใช่การตั้งค่า")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path,
                        default=ROOT / "bot" / "code" / "journal.jsonl")
    args = parser.parse_args()

    events = load(args.journal)
    if not events:
        print(f"ไม่พบ journal หรือไฟล์ว่าง: {args.journal}")
        return
    trades = closed_trades(events)
    if not trades:
        print(f"journal มี {len(events)} events แต่ยังไม่มีไม้ที่ปิดแล้ว")
        return

    by_timeframe: dict[str, list[dict]] = defaultdict(list)
    for trade in trades:
        by_timeframe[trade.get("timeframe") or "?"].append(trade)

    overall = summarise(trades)
    print(f"\nforward evidence จาก {args.journal.name} — {overall['n']} ไม้ที่ปิดแล้ว")
    print(f"ช่วงเวลา {trades[0].get('at', '?')} -> {trades[-1].get('at', '?')}\n")

    header = "%-6s %6s %9s %11s %11s %9s %11s"
    cost_cell = lambda stats: (f"{stats['cost_r']:.4f}R" if stats["cost_r"] is not None
                               else "—")
    print(header % ("TF", "ไม้", "net R", "จริง", "holdout", "ต่าง", "ต้นทุน/ไม้"))
    for timeframe in sorted(by_timeframe):
        stats = summarise(by_timeframe[timeframe])
        promised = holdout_expectancy(timeframe)
        gap = f"{stats['expectancy_r'] - promised:+.3f}" if promised is not None else "—"
        print(header % (
            timeframe, stats["n"], f"{stats['net_r']:+.2f}",
            f"{stats['expectancy_r']:+.4f}",
            f"{promised:+.4f}" if promised is not None else "—",
            gap, cost_cell(stats)))
        if stats["cost_r"] is not None and stats["cost_r"] > COST_R_TRIPWIRE:
            print(f"       [เตือน] ต้นทุนจริง {stats['cost_r']:.3f}R/ไม้ เกิน "
                  f"{COST_R_TRIPWIRE:.2f}R — กินส่วนแบ่งของ edge มากผิดปกติ")
        if stats["priced"] < stats["n"]:
            print(f"       ({stats['n'] - stats['priced']} ไม้ไม่มี risk_cash คู่กัน "
                  "— เปิดก่อนบอทเริ่มบันทึก หรือ journal ถูกตัด)")

    print(header % ("รวม", overall["n"], f"{overall['net_r']:+.2f}",
                    f"{overall['expectancy_r']:+.4f}", "—", "—",
                    cost_cell(overall)))
    print(f"\nwin rate รวม {overall['win_rate']:.1f}%")

    label, advice = verdict(overall["n"], overall["expectancy_r"])
    print(f"\nสถานะ: {label}\n  {advice}")
    if overall["n"] < MIN_TRADES:
        print(f"  เหลืออีก {MIN_TRADES - overall['n']} ไม้")

    converted = sum(1 for event in events
                    if event.get("event") == "converted_market_opened")
    released = sum(1 for event in events
                   if event.get("event") == "limit_released_for_conversion")
    aborted = sum(1 for event in events
                  if event.get("event") in ("convert_aborted", "convert_cancel_rejected",
                                            "convert_skipped"))
    if converted or released or aborted:
        print(f"\nlimit -> market conversion: เปิด market {converted} · "
              f"release limit {released} · ยกเลิกกลางคัน {aborted}")
        if aborted:
            print("  สาเหตุที่ยกเลิกมีทั้ง fill/cancel race, history ยังไม่ยืนยัน "
                  "และ exposure ที่ไม่มี state — ตรวจ event รายตัว")


if __name__ == "__main__":
    main()
