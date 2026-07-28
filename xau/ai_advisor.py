"""OpenRouter-powered advisory filter for Quantum trade plans.

The model can confirm a plan or block it with WAIT. It never executes trades,
never receives MT5 account metadata, and cannot reverse the deterministic
Quantum engine by itself.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd

from . import config

BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-5-mini"
PROMPT_VERSION = "quantum-review-v1"
MIN_CONFIDENCE = 65
TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 200_000

_LOCK = threading.RLock()
_MEMORY_CACHE: dict[str, dict] = {}


class AIUnavailable(RuntimeError):
    """OpenRouter is not configured."""


class AIAdvisorError(RuntimeError):
    """OpenRouter failed or returned an invalid decision."""


DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["BUY", "SELL", "WAIT"]},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "reasons": {"type": "array", "items": {"type": "string"},
                    "minItems": 1, "maxItems": 4},
        "risk_flags": {"type": "array", "items": {"type": "string"},
                       "maxItems": 5},
        "summary_th": {"type": "string"},
    },
    "required": ["action", "confidence", "reasons", "risk_flags", "summary_th"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are a conservative trade-plan reviewer, not a broker and not an order executor.
Use only the supplied closed-bar market snapshot. Review whether the current Quantum plan is supported.
Return BUY or SELL only when evidence supports that direction; otherwise return WAIT.
Never invent prices, indicators, news, fills, or guarantees. Treat conflicting trend/structure, weak scores,
poor historical target performance, stale/no plan, and unresolved risk as reasons to WAIT.
Keep reasons concise and write summary_th in Thai. Output only the required JSON schema."""


def status() -> dict:
    return {
        "configured": bool(os.getenv("OPENROUTER_API_KEY", "").strip()),
        "provider": "OpenRouter",
        "model": os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL),
        "prompt_version": PROMPT_VERSION,
        "mode": "ADVISORY_ONLY",
        "minimum_confidence": MIN_CONFIDENCE,
    }


def market_snapshot(payload: dict) -> dict:
    """Whitelist market fields; intentionally exclude account, server and tick."""
    q = payload.get("quantum") or {}
    setup = payload.get("setup") or None
    stats = payload.get("stats") or {}
    bias = payload.get("bias") or {}
    return {
        "symbol": str(payload.get("symbol", "")),
        "timeframe": str(payload.get("timeframe", "")),
        "closed_bar_time": str(bias.get("time", "")),
        "price": bias.get("price"),
        "market": {
            "bias": q.get("market_bias"), "flow": q.get("market_state"),
            "structure": q.get("structure"), "htf_timeframe": q.get("htf_timeframe"),
            "long_score": q.get("long_score"), "short_score": q.get("short_score"),
            "required_score": q.get("required_score"), "atr": q.get("atr"),
            "rsi": q.get("rsi"), "vwap": q.get("vwap"), "cvd": q.get("cvd"),
            "filters": q.get("filters", []),
        },
        "quantum_plan": setup,
        "backtest": {
            "filled": stats.get("trades"), "tp_win_rates": stats.get("tp_win_rates"),
            "sl_hits": stats.get("sl_hits"), "timeouts": stats.get("timeouts"),
        },
    }


def _cache_key(snapshot: dict, model: str) -> str:
    raw = json.dumps({"prompt": PROMPT_VERSION, "model": model, "snapshot": snapshot},
                     sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise AIAdvisorError("AI response is not a JSON object")
    if set(raw) != set(DECISION_SCHEMA["required"]):
        raise AIAdvisorError("AI response fields do not match the decision schema")
    action = raw.get("action")
    confidence = raw.get("confidence")
    reasons, flags, summary = raw.get("reasons"), raw.get("risk_flags"), raw.get("summary_th")
    if action not in ("BUY", "SELL", "WAIT"):
        raise AIAdvisorError("AI returned an invalid action")
    if isinstance(confidence, bool) or not isinstance(confidence, int) or not 0 <= confidence <= 100:
        raise AIAdvisorError("AI returned an invalid confidence")
    if not isinstance(reasons, list) or not 1 <= len(reasons) <= 4 or not all(isinstance(x, str) and x.strip() for x in reasons):
        raise AIAdvisorError("AI returned invalid reasons")
    if not isinstance(flags, list) or len(flags) > 5 or not all(isinstance(x, str) for x in flags):
        raise AIAdvisorError("AI returned invalid risk flags")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 800:
        raise AIAdvisorError("AI returned an invalid Thai summary")
    return {"action": action, "confidence": confidence,
            "reasons": [x.strip() for x in reasons],
            "risk_flags": [x.strip() for x in flags if x.strip()],
            "summary_th": summary.strip()}


def _request_body(snapshot: dict, model: str) -> dict:
    # Keep production ZDR-style routing by default. Historical replays may
    # explicitly opt in for a model whose only endpoint is not marked private;
    # those snapshots contain public market data only.
    data_policy = ("allow" if os.getenv("OPENROUTER_ALLOW_DATA_COLLECTION", "") == "1"
                   else "deny")
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(snapshot, ensure_ascii=False,
                                                       sort_keys=True, separators=(",", ":"))},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "trade_review", "strict": True,
                            "schema": DECISION_SCHEMA},
        },
        # Kimi K3 exposes max_tokens and does not expose seed. The pinned model
        # and persisted decision log provide auditability without filtering its
        # only endpoint out via require_parameters.
        "max_tokens": 1200,
        "reasoning": {"effort": "low", "exclude": True},
        "provider": {"require_parameters": True, "data_collection": data_policy},
    }


def _call_openrouter(body: dict) -> dict:
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise AIUnavailable("OPENROUTER_API_KEY is not configured")
    request = Request(BASE_URL, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "HTTP-Referer": "http://127.0.0.1:8050", "X-OpenRouter-Title": "HTF Quantum Advisor",
    })
    last_error = None
    for attempt in range(2):
        try:
            with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise AIAdvisorError("OpenRouter response exceeded the size limit")
                return json.loads(raw.decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", errors="replace")
            last_error = AIAdvisorError(f"OpenRouter HTTP {exc.code}: {detail}")
            if exc.code not in (429, 500, 502, 503, 529) or attempt:
                raise last_error from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = AIAdvisorError(f"OpenRouter request failed: {exc}")
            if attempt:
                raise last_error from exc
        time.sleep(0.5)
    raise last_error or AIAdvisorError("OpenRouter request failed")


def _extract(api_response: dict) -> tuple[dict, dict]:
    try:
        choice = api_response["choices"][0]
        content = choice["message"]["content"]
        if isinstance(content, dict):
            decision = content
        elif isinstance(content, list):
            text_parts = [part.get("text", "") for part in content if isinstance(part, dict)]
            decision = json.loads("".join(text_parts))
        else:
            decision = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        finish = ((api_response.get("choices") or [{}])[0]).get("finish_reason")
        usage = api_response.get("usage") or {}
        raise AIAdvisorError(
            "OpenRouter returned an unreadable completion "
            f"(finish={finish}, completion_tokens={usage.get('completion_tokens')}, "
            f"cost={usage.get('cost')})"
        ) from exc
    usage = api_response.get("usage") or {}
    meta = {"request_id": api_response.get("id"), "usage": {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cost": usage.get("cost"),
    }}
    return _validate(decision), meta


def _persist(record: dict) -> None:
    config.ensure_dirs()
    path: Path = config.AI_DECISION_DIR / "openrouter_decisions.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def load_decision_records() -> list[dict]:
    """Load valid persisted decisions; tolerate a truncated final JSONL row."""
    path: Path = config.AI_DECISION_DIR / "openrouter_decisions.jsonl"
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
                if isinstance(record.get("snapshot"), dict) and isinstance(record.get("decision"), dict):
                    records.append(record)
            except (json.JSONDecodeError, AttributeError):
                continue
    return records


def cached_backtest(df, quantum: dict, symbol: str, timeframe: str) -> dict:
    """Compare base plans with the earliest cached AI verdict per signal.

    This never calls OpenRouter. Coverage grows as forward decisions are saved.
    """
    symbol, timeframe = symbol.upper(), timeframe.upper()
    earliest: dict[str, dict] = {}
    for record in sorted(load_decision_records(), key=lambda x: x.get("recorded_at", 0)):
        snap, decision = record["snapshot"], record["decision"]
        if str(snap.get("symbol", "")).upper() != symbol or str(snap.get("timeframe", "")).upper() != timeframe:
            continue
        signal_time = str((snap.get("quantum_plan") or {}).get("signal_time") or "")
        if signal_time and signal_time not in earliest:
            earliest[signal_time] = decision

    filled = [p for p in quantum["plans"] if p["entry_fill_index"] is not None]
    matched, approved = [], []
    for plan in filled:
        signal_time = str(pd.Timestamp(quantum["data"]["time"].iloc[plan["signal_index"]]))
        decision = earliest.get(signal_time)
        if decision:
            matched.append(plan)
            if decision.get("approved"):
                approved.append(plan)

    point = 10 ** -int(config.symbol_cfg(symbol)["decimals"])
    def metrics(plans: list[dict]) -> dict:
        targets = {}
        for k, (label, rr) in enumerate((("TP1", 1.0), ("TP2", 1.5), ("TP3", 2.0))):
            wins = sum(p["resolved"][k] == "tp" for p in plans)
            losses = sum(p["resolved"][k] == "sl" for p in plans)
            unresolved = len(plans) - wins - losses
            spread_r = sum(float(df.iloc[p["entry_fill_index"]].get("spread", 0) or 0) * point /
                           float(p["risk"]) for p in plans if p["risk"])
            resolved = wins + losses
            targets[label] = {"wins": wins, "losses": losses, "unresolved": unresolved,
                              "win_rate": round(wins / resolved * 100, 2) if resolved else None,
                              "spread_adjusted_r": round(wins * rr - losses - spread_r, 3)}
        return {"plans": len(plans), "targets": targets}

    return {
        "symbol": symbol, "timeframe": timeframe, "prompt_version": PROMPT_VERSION,
        "model": os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL),
        "coverage": {"filled_plans": len(filled), "ai_reviewed": len(matched),
                     "ai_approved": len(approved),
                     "percent": round(len(matched) / len(filled) * 100, 2) if filled else 0},
        "base": metrics(filled), "ai_filter": metrics(approved),
        "warning": "Forward decisions only; do not interpret low-coverage results as a historical AI backtest.",
    }


def advise(payload: dict, *, transport: Callable[[dict], dict] | None = None,
           force: bool = False, persist: bool = True) -> dict:
    snapshot = market_snapshot(payload)
    model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    key = _cache_key(snapshot, model)
    with _LOCK:
        if not force and key in _MEMORY_CACHE:
            return _MEMORY_CACHE[key] | {"cached": True}

    api_response = (transport or _call_openrouter)(_request_body(snapshot, model))
    decision, meta = _extract(api_response)
    setup = snapshot.get("quantum_plan") or {}
    base_action = setup.get("action") if setup.get("ok") else "WAIT"
    agrees = decision["action"] == base_action
    approved = (base_action in ("BUY", "SELL") and agrees
                and decision["confidence"] >= MIN_CONFIDENCE
                and not decision["risk_flags"])
    result = {
        **decision, "effective_action": base_action if approved else "WAIT",
        "quantum_action": base_action, "agrees_with_quantum": agrees,
        "approved": approved, "model": model, "provider": "OpenRouter",
        "prompt_version": PROMPT_VERSION, "decision_id": key,
        "bar_time": snapshot["closed_bar_time"], "symbol": snapshot["symbol"],
        "timeframe": snapshot["timeframe"], "cached": False, **meta,
    }
    record = {"recorded_at": time.time(), "snapshot": snapshot, "decision": result}
    with _LOCK:
        _MEMORY_CACHE[key] = result
        if persist:
            _persist(record)
    return result
