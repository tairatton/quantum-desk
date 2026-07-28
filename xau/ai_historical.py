"""Point-in-time OpenRouter replay for completed Quantum plans.

Every AI request is reconstructed from the signal bar and earlier bars only.
Trade outcomes are joined after inference and are never included in the prompt.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

from . import ai_advisor, config, quantum as quantum_engine

_WRITE_LOCK = threading.Lock()


def _result_path(symbol: str, timeframe: str, model: str) -> Path:
    safe_model = re.sub(r"[^a-zA-Z0-9._-]+", "_", model)
    return config.AI_DECISION_DIR / (
        f"historical_{safe_model}_{symbol.lower()}_{timeframe.lower()}.jsonl"
    )


def _signal_time(data: pd.DataFrame, plan: dict) -> str:
    return str(pd.Timestamp(data["time"].iloc[plan["signal_index"]]))


def _payload_at_signal(result: dict, plan: dict, symbol: str, timeframe: str) -> dict:
    """Build the same advisor payload without exposing later bars or outcomes."""
    data = result["data"]
    index = int(plan["signal_index"])
    history = data.iloc[:index + 1]
    row = history.iloc[-1]
    direction = int(plan["direction"])
    action = "BUY" if direction == 1 else "SELL"
    immediate = plan["entry_fill_index"] == index
    market_bias = "Bullish" if row["close"] > row["session_vwap"] else "Bearish"
    market_state = (
        "INST. BUYING" if row["close"] > row["session_vwap"] and row["cvd"] > 0
        else "INST. SELLING" if row["close"] < row["session_vwap"] and row["cvd"] < 0
        else "NEUTRAL/TRAP"
    )
    break_level = (round(float(row["break_level"]), 2)
                   if pd.notna(row["break_level"]) else None)
    setup = {
        "ok": True, "action": action,
        "status": f"{action} ACTIVE" if immediate else f"{action} WAIT ENTRY",
        "position": "LONG" if direction == 1 else "SHORT",
        "lifecycle": "ACTIVE" if immediate else "WAIT ENTRY",
        "signal_time": _signal_time(data, plan),
        "fill_time": _signal_time(data, plan) if immediate else None,
        "entry": round(float(plan["entry"]), 2),
        "stop": round(float(plan["stop"]), 2),
        "risk": round(float(plan["risk"]), 2),
        "targets": [
            {"label": f"TP{k + 1}", "price": round(float(price), 2), "rr": rr}
            for k, (price, rr) in enumerate(zip(plan["targets"], quantum_engine.RR_TARGETS))
        ],
    }
    return {
        "symbol": symbol.upper(), "timeframe": timeframe.upper(), "setup": setup,
        "bias": {"time": _signal_time(data, plan), "price": round(float(row["close"]), 2)},
        "quantum": {
            "market_bias": market_bias, "market_state": market_state,
            "structure": {
                "direction": "Bullish" if int(row["structure_state"]) == 1 else "Bearish",
                "kind": str(row["break_kind"] or "BOS"), "level": break_level,
            },
            "htf_timeframe": data.attrs.get("htf_label", timeframe.upper()),
            "long_score": int(row["long_score"]), "short_score": int(row["short_score"]),
            "required_score": 6, "atr": round(float(row["atr"]), 2),
            "rsi": round(float(row["rsi"]), 1),
            "vwap": round(float(row["session_vwap"]), 2),
            "cvd": round(float(row["cvd"]), 0),
            "filters": quantum_engine._filter_snapshot(history, direction),
        },
        # Deliberately empty: full-sample win rates would leak future outcomes.
        "stats": {},
    }


def _load(path: Path) -> dict[str, dict]:
    records: dict[str, dict] = {}
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
            signal_time = record.get("signal_time")
            if signal_time and isinstance(record.get("decision"), dict):
                records.setdefault(signal_time, record)
        except (json.JSONDecodeError, AttributeError):
            continue
    return records


def _append(path: Path, record: dict) -> None:
    with _WRITE_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def _metrics(plans: list[dict], data: pd.DataFrame, symbol: str) -> dict:
    point = 10 ** -int(config.symbol_cfg(symbol)["decimals"])
    spread_r = sum(
        float(data.iloc[plan["entry_fill_index"]].get("spread", 0) or 0) * point /
        float(plan["risk"])
        for plan in plans if plan.get("risk") and plan.get("entry_fill_index") is not None
    )
    targets = {}
    for k, (label, rr) in enumerate(zip(("TP1", "TP2", "TP3"), quantum_engine.RR_TARGETS)):
        wins = sum(plan["resolved"][k] == "tp" for plan in plans)
        losses = sum(plan["resolved"][k] == "sl" for plan in plans)
        resolved = wins + losses
        targets[label] = {
            "wins": wins, "losses": losses, "unresolved": len(plans) - resolved,
            "win_rate": round(wins / resolved * 100, 2) if resolved else None,
            "gross_r": round(wins * rr - losses, 2),
            "spread_adjusted_r": round(wins * rr - losses - spread_r, 3),
        }
    return {"plans": len(plans), "targets": targets}


def run(data: pd.DataFrame, result: dict, symbol: str, timeframe: str,
        *, limit: int | None = None, workers: int = 2, progress=None) -> dict:
    """Replay filled plans through OpenRouter, checkpointing every response."""
    model = os.getenv("OPENROUTER_MODEL", ai_advisor.DEFAULT_MODEL).strip()
    path = _result_path(symbol, timeframe, model)
    config.ensure_dirs()
    existing = _load(path)
    filled = [plan for plan in result["plans"] if plan["entry_fill_index"] is not None]
    selected = filled[:limit] if limit else filled
    pending = [plan for plan in selected if _signal_time(result["data"], plan) not in existing]

    def review(plan: dict) -> dict:
        payload = _payload_at_signal(result, plan, symbol, timeframe)
        decision = ai_advisor.advise(payload, force=True, persist=False)
        record = {
            "recorded_at": time.time(), "mode": "historical_point_in_time",
            "symbol": symbol.upper(), "timeframe": timeframe.upper(), "model": model,
            "signal_time": payload["setup"]["signal_time"],
            "snapshot": ai_advisor.market_snapshot(payload), "decision": decision,
        }
        _append(path, record)
        return record

    errors = []
    if pending:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as pool:
            futures = {pool.submit(review, plan): plan for plan in pending}
            for completed, future in enumerate(as_completed(futures), 1):
                try:
                    record = future.result()
                    existing[record["signal_time"]] = record
                    if progress:
                        progress(completed, len(pending), record)
                except Exception as exc:  # checkpoint successes and report failures
                    errors.append(str(exc))
                    if progress:
                        progress(completed, len(pending), {"error": str(exc)})

    reviewed, approved, agreement_65 = [], [], []
    decisions = []
    for plan in selected:
        record = existing.get(_signal_time(result["data"], plan))
        if not record:
            continue
        reviewed.append(plan)
        decisions.append(record["decision"])
        if record["decision"].get("approved"):
            approved.append(plan)
        base_action = "BUY" if plan["direction"] == 1 else "SELL"
        decision = record["decision"]
        if (decision.get("action") == base_action and
                float(decision.get("confidence") or 0) >= ai_advisor.MIN_CONFIDENCE):
            agreement_65.append(plan)
    actual_cost = sum(float((decision.get("usage") or {}).get("cost") or 0)
                      for decision in decisions)
    confidences = [decision.get("confidence") for decision in decisions
                   if isinstance(decision.get("confidence"), (int, float))]
    action_counts = {action: sum(d.get("action") == action for d in decisions)
                     for action in ("BUY", "SELL", "WAIT")}
    report = {
        "symbol": symbol.upper(), "timeframe": timeframe.upper(), "model": model,
        "method": "point-in-time replay; outcomes excluded from AI prompts",
        "coverage": {
            "eligible": len(selected), "reviewed": len(reviewed),
            "approved": len(approved),
            "percent": round(len(reviewed) / len(selected) * 100, 2) if selected else 0,
        },
        "decisions": {
            "actions": action_counts,
            "average_confidence": round(sum(confidences) / len(confidences), 2) if confidences else None,
            "risk_flagged": sum(bool(d.get("risk_flags")) for d in decisions),
        },
        "base": _metrics(selected, data, symbol),
        "ai_filter_strict": _metrics(approved, data, symbol),
        "ai_agreement_65": _metrics(agreement_65, data, symbol),
        "policy_note": (
            "ai_filter_strict requires matching direction, confidence >=65 and no risk flags; "
            "ai_agreement_65 treats risk flags as warnings but still requires matching direction and confidence >=65."
        ),
        "actual_cost_usd": round(actual_cost, 6), "errors": errors,
        "decision_log": str(path),
    }
    report_path = (
        config.AI_REPORT_DIR / "historical" / symbol.upper()
        / timeframe.upper() / "report.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report
