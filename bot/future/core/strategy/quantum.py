"""Python port of the supplied HTF Quantum Adaptive Pine trade engine.

Defaults mirror the Pine script: Adaptive Structure signals, Balanced 6/9
quality filter, adaptive close/retrace entry, structure stop with ATR bounds,
and fixed 1R/1.5R/2R targets.
"""
from __future__ import annotations

import pandas as pd

from .indicators import atr, rsi

FRACTAL_LENGTH = 11
SETUP_WINDOW = 8
ENTRY_TIMEOUT = 16
# Bars to wait for the 50% retrace before taking the trade at market instead.
# `None` restores the original behaviour: wait the full `ENTRY_TIMEOUT` and let
# the plan expire unfilled.
#
# Waiting is adverse selection. A limit that never fills is a limit the price ran
# away from, so the plans that expire are the strongest ones: measured over
# 50,000 bars, the expired setups were worth +1.005R (M15) and +0.646R (M30) per
# trade against the +0.011..+0.284R the filled ones actually earned. Converting
# only the stragglers keeps the better fill price on the limits that do trigger,
# which is why this beats entering at market every time (see
# docs/ENTRY_TIMING_EXPERIMENT_2026-07-31.md).
#
# The floor is one bar, not zero: the signal bar creates the plan and the loop
# moves on, so 0 and any negative value behave exactly like 1. A plan that should
# be taken on its own bar is already handled — `_new_plan` marks it immediate
# when the break is within 0.75 ATR and no limit is placed at all.
CONVERT_TO_MARKET_BARS: int | None = 2
MAX_TRADE_BARS = 120
COOLDOWN = 2
# Operational default requested for the dashboard: release an active plan when
# a confirmed opposite BOS/CHoCH appears. This is equivalent to enabling Pine's
# "End Active Plan on Opposite Structure" input and does not count as a TP loss.
INVALIDATE_ACTIVE_ON_OPPOSITE = True
MIN_STOP_ATR = 0.8
MAX_STOP_ATR = 2.5
ATR_BUFFER = 0.20
RR_TARGETS = (1.0, 1.5, 2.0)


def _volume(df: pd.DataFrame) -> pd.Series:
    if "real_volume" in df and float(df["real_volume"].fillna(0).sum()) > 0:
        return df["real_volume"].fillna(0).astype(float)
    if "tick_volume" in df:
        return df["tick_volume"].fillna(0).astype(float)
    return pd.Series(1.0, index=df.index)


def _adx_di(df: pd.DataFrame, period: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    high, low, close = df["high"], df["low"], df["close"]
    up, down = high.diff(), -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
    smooth_tr = tr.ewm(alpha=1/period, adjust=False).mean()
    plus = 100 * plus_dm.ewm(alpha=1/period, adjust=False).mean() / smooth_tr
    minus = 100 * minus_dm.ewm(alpha=1/period, adjust=False).mean() / smooth_tr
    denom = (plus + minus).where((plus + minus) > 0)
    adx = (100 * (plus-minus).abs() / denom).ewm(alpha=1/period, adjust=False).mean()
    return plus.fillna(0), minus.fillna(0), adx.fillna(0)


def _session_metrics(df: pd.DataFrame, rows: int = 24) -> dict:
    d = df.copy(); d["time"] = pd.to_datetime(d["time"])
    session = d[d["time"].dt.date == d["time"].iloc[-1].date()].copy()
    if session.empty: session = d.tail(1).copy()
    volume = _volume(session)
    typical = (session["high"] + session["low"] + session["close"]) / 3
    total = float(volume.sum())
    vwap = float((typical*volume).sum()/total) if total > 0 else float(session["close"].iloc[-1])
    signed = volume.where(session["close"] >= session["open"], -volume)
    cvd = signed.cumsum(); cvd_now = float(cvd.iloc[-1])
    cvd_then = float(cvd.iloc[-4]) if len(cvd) >= 4 else float(cvd.iloc[0])
    low, high = float(session["low"].min()), float(session["high"].max())
    span = max(high-low, 1e-9); buckets = [0.0] * rows
    for price, amount in zip(session["close"].astype(float), volume):
        index = min(rows-1, max(0, int((price-low)/span*rows)))
        buckets[index] += float(amount)
    poc = max(range(rows), key=buckets.__getitem__)
    left = right = poc; covered = buckets[poc]; target = total*.70
    while covered < target and (left > 0 or right < rows-1):
        below = buckets[left-1] if left > 0 else -1
        above = buckets[right+1] if right < rows-1 else -1
        if above > below: right += 1; covered += buckets[right]
        else: left -= 1; covered += buckets[left]
    step = span/rows
    return {"vwap": round(vwap,2), "cvd": round(cvd_now,0),
            "cvd_rising": cvd_now > cvd_then, "cvd_falling": cvd_now < cvd_then,
            "poc": round(low+(poc+.5)*step,2), "vah": round(low+(right+1)*step,2),
            "val": round(low+left*step,2), "range_high": round(high,2),
            "range_low": round(low,2), "range_mid": round((high+low)/2,2)}


def _confirmed_htf(d: pd.DataFrame, timeframe: str) -> tuple[pd.Series, pd.Series, str]:
    """Closed HTF EMA20/50 mapped onto each chart bar without look-ahead."""
    tf = timeframe.upper()
    if tf in ("M1", "M5", "M15", "M30"):
        indexed = d.set_index("time")["close"]
        htf = indexed.resample("1h", label="left", closed="left").last().dropna()
        fast = htf.ewm(span=20, adjust=False).mean().shift(1)
        slow = htf.ewm(span=50, adjust=False).mean().shift(1)
        times = pd.DatetimeIndex(d["time"])
        bull = (fast > slow).reindex(times, method="ffill").fillna(False)
        bear = (fast < slow).reindex(times, method="ffill").fillna(False)
        return (pd.Series(bull.to_numpy(), index=d.index),
                pd.Series(bear.to_numpy(), index=d.index), "H1")
    # Pine's default signal HTF is 60m. On charts >= H1, using the previous
    # fully closed chart bar is the deterministic fallback available in MT5.
    fast = d["close"].ewm(span=20, adjust=False).mean().shift(1)
    slow = d["close"].ewm(span=50, adjust=False).mean().shift(1)
    return (fast > slow).fillna(False), (fast < slow).fillna(False), tf


def _features(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    d = df.reset_index(drop=True).copy()
    d["time"] = pd.to_datetime(d["time"])
    d["atr"] = atr(d, 14)
    d["ema20"] = d["close"].ewm(span=20, adjust=False).mean()
    d["ema50"] = d["close"].ewm(span=50, adjust=False).mean()
    d["rsi"] = rsi(d, 14)["rsi_14"]
    d["volume"] = _volume(d)
    d["volume_avg"] = d["volume"].rolling(20).mean()
    d["plus_di"], d["minus_di"], d["adx"] = _adx_di(d)
    day = d["time"].dt.date
    typical = (d["high"] + d["low"] + d["close"]) / 3
    d["session_vwap"] = ((typical * d["volume"]).groupby(day).cumsum() /
                         d["volume"].groupby(day).cumsum().replace(0, float("nan")))
    signed = d["volume"].where(d["close"] >= d["open"], -d["volume"])
    d["cvd"] = signed.groupby(day).cumsum()
    d["htf_bull"], d["htf_bear"], htf_label = _confirmed_htf(d, timeframe)
    d.attrs["htf_label"] = htf_label

    n, p = len(d), FRACTAL_LENGTH // 2
    up_level = down_level = None
    up_broken = down_broken = False
    state = 0
    states, ups, downs, events, kinds, levels = [], [], [], [], [], []
    for bar in range(n):
        pivot = bar - p
        if pivot >= p and pivot + p < n:
            hi = float(d["high"].iloc[pivot])
            lo = float(d["low"].iloc[pivot])
            if hi >= float(d["high"].iloc[pivot-p:pivot+p+1].max()):
                up_level, up_broken = hi, False
            if lo <= float(d["low"].iloc[pivot-p:pivot+p+1].min()):
                down_level, down_broken = lo, False
        event, kind, level = 0, "", float("nan")
        close = float(d["close"].iloc[bar])
        if up_level is not None and not up_broken and close > up_level:
            event, kind, level = 1, "CHoCH" if state == -1 else "BOS", up_level
            up_broken, state = True, 1
        if down_level is not None and not down_broken and close < down_level:
            event, kind, level = -1, "CHoCH" if state == 1 else "BOS", down_level
            down_broken, state = True, -1
        states.append(state); ups.append(up_level); downs.append(down_level)
        events.append(event); kinds.append(kind); levels.append(level)
    d["structure_state"], d["up_level"], d["down_level"] = states, ups, downs
    d["break_event"], d["break_kind"], d["break_level"] = events, kinds, levels

    body = (d["close"] - d["open"]).abs()
    common_volume = d["volume_avg"].isna() | (d["volume"] >= d["volume_avg"] * .90)
    common_adx = d["adx"] >= 12
    near_vwap = (d["close"] - d["session_vwap"]).abs() <= d["atr"] * 2
    d["long_score"] = (
        d["htf_bull"].astype(int) + ((d["close"] > d["ema20"]) & (d["ema20"] > d["ema50"])).astype(int) +
        ((d["close"] > d["open"]) & (body >= d["atr"] * .10)).astype(int) + common_volume.astype(int) +
        common_adx.astype(int) + (d["plus_di"] > d["minus_di"]).astype(int) +
        ((d["close"] >= d["session_vwap"]) & near_vwap).astype(int) +
        (d["cvd"] > d["cvd"].shift(3)).astype(int) + (d["rsi"] >= 50).astype(int)
    )
    d["short_score"] = (
        d["htf_bear"].astype(int) + ((d["close"] < d["ema20"]) & (d["ema20"] < d["ema50"])).astype(int) +
        ((d["close"] < d["open"]) & (body >= d["atr"] * .10)).astype(int) + common_volume.astype(int) +
        common_adx.astype(int) + (d["minus_di"] > d["plus_di"]).astype(int) +
        ((d["close"] <= d["session_vwap"]) & near_vwap).astype(int) +
        (d["cvd"] < d["cvd"].shift(3)).astype(int) + (d["rsi"] <= 50).astype(int)
    )
    d["long_trend"] = d["htf_bull"] | ((d["close"] > d["ema20"]) & (d["ema20"] > d["ema50"]))
    d["short_trend"] = d["htf_bear"] | ((d["close"] < d["ema20"]) & (d["ema20"] < d["ema50"]))
    return d


def _stop_and_risk(d: pd.DataFrame, i: int, direction: int, entry: float) -> tuple[float, float]:
    """Structure stop for `entry`, bounded by the ATR floor and ceiling.

    Shared by `_new_plan` and `_convert_to_market` so a converted entry is priced
    by exactly the same rule as an immediate one — the stop follows the entry,
    which is what keeps 1R the same quantity in both cases.
    """
    a = float(d["atr"].iloc[i])
    structure = d["down_level"].iloc[i] if direction == 1 else d["up_level"].iloc[i]
    structure_valid = pd.notna(structure) and (structure < entry if direction == 1 else structure > entry)
    if not structure_valid:
        lookback = d.iloc[max(0, i-19):i+1]
        structure = float(lookback["low"].min() if direction == 1 else lookback["high"].max())
    raw_stop = float(structure) - direction * a * ATR_BUFFER
    risk = min(max(abs(entry - raw_stop), a * MIN_STOP_ATR), a * MAX_STOP_ATR)
    return entry - direction * risk, risk


def _convert_to_market(plan: dict, d: pd.DataFrame, i: int) -> dict:
    """Take a still-unfilled limit at this bar's close instead of waiting.

    Re-prices stop and targets off the new entry. Mutates and returns `plan` so
    the caller keeps its identity in `plans` — the plan is the same trade idea,
    only the fill is different.
    """
    direction = plan["direction"]
    entry = float(d["close"].iloc[i])
    stop, risk = _stop_and_risk(d, i, direction, entry)
    plan.update(
        # What the plan looked like on its signal bar. Kept because anything
        # reconstructing the decision as it was made — `ai_historical` builds an
        # advisor payload that must not see later bars — would otherwise read
        # these fields and get numbers derived from the conversion bar instead.
        limit_entry=plan["entry"], limit_stop=plan["stop"], limit_risk=plan["risk"],
        limit_targets=list(plan["targets"]),
        entry=entry, stop=stop, risk=risk,
        targets=[entry + direction * risk * rr for rr in RR_TARGETS],
        pending=False, active=True, entry_fill_index=i,
        converted=True,
        status="BUY ACTIVE" if direction == 1 else "SELL ACTIVE",
    )
    return plan


def _new_plan(d: pd.DataFrame, i: int, direction: int, break_level: float) -> dict:
    close, a = float(d["close"].iloc[i]), float(d["atr"].iloc[i])
    valid_break = break_level <= close if direction == 1 else break_level >= close
    distance = abs(close - break_level) if valid_break else 0.0
    immediate = not valid_break or distance <= a * .75
    entry = close if immediate else close - direction * distance * .50
    stop, risk = _stop_and_risk(d, i, direction, entry)
    return {
        "direction": direction, "side": "long" if direction == 1 else "short",
        "signal_index": i, "entry_fill_index": i if immediate else None,
        "entry": entry, "stop": stop, "risk": risk,
        "targets": [entry + direction * risk * rr for rr in RR_TARGETS],
        "resolved": [None, None, None], "pending": not immediate,
        "active": immediate, "converted": False,
        "status": ("BUY ACTIVE" if direction == 1 else "SELL ACTIVE") if immediate
                  else ("BUY WAIT ENTRY" if direction == 1 else "SELL WAIT ENTRY"),
    }


def analyse(df: pd.DataFrame, timeframe: str) -> dict:
    """Run the Pine state machine over all supplied history."""
    if len(df) < 80:
        raise ValueError("Quantum analysis needs at least 80 bars")
    d = _features(df, timeframe)
    plans: list[dict] = []
    current = None
    last_long_bar = last_short_bar = None
    last_long_level = last_short_level = None
    long_used = short_used = False
    last_end = -10_000
    counters = {"filled": 0, "expired": 0, "cancelled": 0, "timed_out": 0,
                "invalidated": 0, "converted": 0, "wins": [0, 0, 0], "losses": [0, 0, 0]}

    for i in range(55, len(d)):
        event = int(d["break_event"].iloc[i])
        if event == 1:
            last_long_bar, last_long_level, long_used = i, float(d["break_level"].iloc[i]), False
        elif event == -1:
            last_short_bar, last_short_level, short_used = i, float(d["break_level"].iloc[i]), False

        long_alive = last_long_bar is not None and i-last_long_bar <= SETUP_WINDOW and not long_used and int(d["structure_state"].iloc[i]) == 1
        short_alive = last_short_bar is not None and i-last_short_bar <= SETUP_WINDOW and not short_used and int(d["structure_state"].iloc[i]) == -1
        long_ready = long_alive and int(d["long_score"].iloc[i]) >= 6 and bool(d["long_trend"].iloc[i])
        short_ready = short_alive and int(d["short_score"].iloc[i]) >= 6 and bool(d["short_trend"].iloc[i])

        # Release the stale plan before evaluating the new candidate. The
        # normal cooldown still applies, matching Pine's lastPlanEndBar rule.
        opposite_ready = (short_ready if current is not None and current["direction"] == 1
                          else long_ready)
        if (INVALIDATE_ACTIVE_ON_OPPOSITE and current is not None and current["active"]
                and event == -current["direction"] and opposite_ready):
            old_action = "BUY" if current["direction"] == 1 else "SELL"
            current["active"] = False
            current["status"] = f"{old_action} INVALIDATED"
            counters["invalidated"] += 1
            last_end = i

        can_accept = (current is None or (not current["pending"] and not current["active"])) and i-last_end > COOLDOWN
        created = False
        if can_accept and (long_ready or short_ready):
            direction = 1 if long_ready and (not short_ready or int(d["structure_state"].iloc[i]) == 1 or d["long_score"].iloc[i] > d["short_score"].iloc[i]) else -1
            level = last_long_level if direction == 1 else last_short_level
            current = _new_plan(d, i, direction, float(level))
            plans.append(current); created = True
            if direction == 1: long_used = True
            else: short_used = True
            if current["active"]: counters["filled"] += 1

        if current is None or created or (not current["pending"] and not current["active"]):
            continue
        direction = current["direction"]
        opposite = event == -direction
        if current["pending"] and opposite:
            current["pending"] = False; current["status"] = "CANCELLED"
            counters["cancelled"] += 1; last_end = i; continue
        if current["pending"] and i-current["signal_index"] > ENTRY_TIMEOUT:
            current["pending"] = False; current["status"] = "EXPIRED"
            counters["expired"] += 1; last_end = i; continue
        if current["active"] and i-current["entry_fill_index"] > MAX_TRADE_BARS:
            current["active"] = False; current["status"] = "TIMEOUT"
            counters["timed_out"] += 1; last_end = i; continue

        high, low, close = map(float, (d["high"].iloc[i], d["low"].iloc[i], d["close"].iloc[i]))
        if current["pending"]:
            touched = low <= current["entry"] if direction == 1 else high >= current["entry"]
            if touched:
                current["pending"], current["active"] = False, True
                current["entry_fill_index"] = i; current["status"] = "BUY ACTIVE" if direction == 1 else "SELL ACTIVE"
                counters["filled"] += 1
            elif (CONVERT_TO_MARKET_BARS is not None
                    and i - current["signal_index"] >= CONVERT_TO_MARKET_BARS):
                # The retrace has not come. Take it at market rather than let the
                # plan expire — the opposite-structure cancel above still had its
                # say first, so only the stragglers convert, never the reversals.
                _convert_to_market(current, d, i)
                counters["filled"] += 1
                counters["converted"] += 1
            continue  # Pine does not resolve TP/SL on the fill candle

        stop_hit = low <= current["stop"] if direction == 1 else high >= current["stop"]
        target_hits = [high >= t if direction == 1 else low <= t for t in current["targets"]]
        if stop_hit:
            for k in range(3):
                if current["resolved"][k] is None:
                    current["resolved"][k] = "sl"; counters["losses"][k] += 1
            current["active"] = False; current["status"] = "BUY SL" if direction == 1 else "SELL SL"
            last_end = i; continue
        for k, hit in enumerate(target_hits):
            if hit and current["resolved"][k] is None:
                current["resolved"][k] = "tp"; counters["wins"][k] += 1
        if target_hits[2]:
            current["active"] = False; current["status"] = "BUY TP3" if direction == 1 else "SELL TP3"; last_end = i
        elif target_hits[1]: current["status"] = "BUY TP2 ACTIVE" if direction == 1 else "SELL TP2 ACTIVE"
        elif target_hits[0]: current["status"] = "BUY TP1 ACTIVE" if direction == 1 else "SELL TP1 ACTIVE"

    rates = {}
    for k, label in enumerate(("TP1", "TP2", "TP3")):
        resolved = counters["wins"][k] + counters["losses"][k]
        rates[label] = round(counters["wins"][k] / resolved * 100, 2) if resolved else None
    latest = d.iloc[-1]
    session = _session_metrics(d)
    market_bias = "Bullish" if latest["close"] > latest["session_vwap"] else "Bearish"
    market_state = "INST. BUYING" if latest["close"] > latest["session_vwap"] and latest["cvd"] > 0 else (
        "INST. SELLING" if latest["close"] < latest["session_vwap"] and latest["cvd"] < 0 else "NEUTRAL/TRAP")
    selected_dir = current["direction"] if current else int(latest["structure_state"])
    filters = _filter_snapshot(d, selected_dir)
    return {"data": d, "plan": current, "plans": plans, "rates": rates,
            "counts": counters, "market_bias": market_bias, "market_state": market_state,
            "price": round(float(latest["close"]), 2), "atr": round(float(latest["atr"]), 2),
            "rsi": round(float(latest["rsi"]), 1), "htf_timeframe": d.attrs["htf_label"],
            "long_score": int(latest["long_score"]), "short_score": int(latest["short_score"]),
            "required_score": 6, "structure": {"direction": "Bullish" if latest["structure_state"] == 1 else "Bearish",
            "kind": str(latest["break_kind"] or "BOS"), "level": round(float(latest["break_level"]), 2) if pd.notna(latest["break_level"]) else None},
            "filters": filters, **session}


def _filter_snapshot(d: pd.DataFrame, direction: int) -> list[dict]:
    x = d.iloc[-1]; long = direction != -1
    body = abs(float(x["close"]-x["open"])) >= float(x["atr"])*.10
    values = {
        "HTF EMA": bool(x["htf_bull"] if long else x["htf_bear"]),
        "Local EMA": bool(x["close"] > x["ema20"] > x["ema50"] if long else x["close"] < x["ema20"] < x["ema50"]),
        "Candle body": bool(body and (x["close"] > x["open"] if long else x["close"] < x["open"])),
        "Volume": bool(pd.isna(x["volume_avg"]) or x["volume"] >= x["volume_avg"]*.90),
        "ADX": bool(x["adx"] >= 12),
        "DI direction": bool(x["plus_di"] > x["minus_di"] if long else x["minus_di"] > x["plus_di"]),
        "VWAP": bool((x["close"] >= x["session_vwap"] if long else x["close"] <= x["session_vwap"]) and abs(x["close"]-x["session_vwap"]) <= x["atr"]*2),
        "CVD slope": bool(x["cvd"] > d["cvd"].shift(3).iloc[-1] if long else x["cvd"] < d["cvd"].shift(3).iloc[-1]),
        "RSI": bool(x["rsi"] >= 50 if long else x["rsi"] <= 50),
    }
    return [{"name": k, "ok": v} for k, v in values.items()]


def public(result: dict) -> dict:
    """Remove internal DataFrames and shape the current Pine plan for JSON."""
    plan = result["plan"]
    setup = None
    if plan:
        action = "BUY" if plan["direction"] == 1 else "SELL"
        lifecycle = "ACTIVE" if plan["active"] else "WAIT ENTRY" if plan["pending"] else "CLOSED"
        signal_time = str(pd.Timestamp(result["data"]["time"].iloc[plan["signal_index"]]))
        fill_time = (str(pd.Timestamp(result["data"]["time"].iloc[plan["entry_fill_index"]]))
                     if plan["entry_fill_index"] is not None else None)
        setup = {"ok": bool(plan["active"] or plan["pending"]), "reason": plan["status"],
                 "status": plan["status"], "side": plan["side"],
                 "action": action, "position": "LONG" if plan["direction"] == 1 else "SHORT",
                 "lifecycle": lifecycle, "is_active": bool(plan["active"]),
                 "is_pending": bool(plan["pending"]),
                 "signal_time": signal_time, "fill_time": fill_time,
                 "entry": round(plan["entry"], 2), "stop": round(plan["stop"], 2),
                 "risk": round(plan["risk"], 2), "risk_pct": round(plan["risk"]/plan["entry"]*100, 2),
                 "kind": "HTF Quantum Adaptive", "targets": [
                     {"label": f"TP{i+1}", "price": round(price, 2), "rr": RR_TARGETS[i],
                      "hit": plan["resolved"][i] == "tp", "win_rate": result["rates"][f"TP{i+1}"],
                      "backtest_hits": result["counts"]["wins"][i]}
                     for i, price in enumerate(plan["targets"])]}
    return {k: v for k, v in result.items() if k not in ("data", "plans", "plan")} | {"setup": setup}
