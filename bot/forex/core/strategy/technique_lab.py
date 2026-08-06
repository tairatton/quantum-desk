"""Deterministic exit-technique lab with chronological holdout evaluation."""
from __future__ import annotations

import itertools
import json
from pathlib import Path

import pandas as pd

from . import config, quantum

PURGE_BARS = quantum.ENTRY_TIMEOUT + quantum.MAX_TRADE_BARS + 5


def _cost_r(data: pd.DataFrame, plan: dict, symbol: str) -> float:
    """Round-trip cost of one trade, in R.

    Replaces `_spread_r`, which charged the entry bar's spread and nothing else.
    The spread itself was right — bars are bid-quoted, so a round trip pays it
    once — but commission and slippage are absent from the feed and were simply
    missing. See `config.COSTS` for the arithmetic and for why the count is one.
    """
    risk = float(plan["risk"])
    if risk <= 0:
        return 0.0
    point = 10 ** -int(config.symbol_cfg(symbol)["decimals"])
    costs = config.cost_cfg(symbol)
    spread = float(data.iloc[plan["entry_fill_index"]].get("spread", 0) or 0)
    price_cost = (spread * costs["spread_sides"]
                  + costs["slippage_points"]) * point
    # Commission is quoted in cash per lot; R is the stop distance in cash, so the
    # lot size cancels and the ratio is size-independent.
    commission_r = (costs["commission_per_lot"]
                    / (risk * costs["value_per_point"])
                    if costs["value_per_point"] else 0.0)
    return price_cost / risk + commission_r


def _spread_r(data: pd.DataFrame, plan: dict, symbol: str) -> float:
    """Deprecated name kept so nothing silently calls the half-cost version."""
    return _cost_r(data, plan, symbol)


def _target_payoff(plan: dict, target: int) -> float:
    outcome = plan["resolved"][target]
    return quantum.RR_TARGETS[target] if outcome == "tp" else -1.0 if outcome == "sl" else 0.0


def _weighted_payoff(plan: dict, weights: tuple[float, float, float],
                     breakeven: bool, step_after_tp2: bool = False) -> float:
    tp1_hit = plan["resolved"][0] == "tp"
    tp2_hit = plan["resolved"][1] == "tp"
    value = 0.0
    for k, weight in enumerate(weights):
        outcome = plan["resolved"][k]
        if outcome == "tp":
            value += weight * quantum.RR_TARGETS[k]
        elif outcome == "sl":
            # Once TP1 is banked, a BE rule protects only the remaining size.
            if breakeven and tp1_hit and k > 0:
                # After TP2, the remaining TP3 leg is protected at TP1 (+1R).
                # The caller supplies the flag because the lab also retains
                # older BE-only techniques for comparison.
                value += (weight if step_after_tp2 and tp2_hit and k == 2
                          else 0.0)
            else:
                value -= weight
    return value


#: Payoff per technique, before cost. One definition, so a script that wants to
#: slice results a different way cannot quietly price a different exit than the
#: report does — `ftmo_portfolio_sim` used to hardcode BE + 33/33/34 here.
TECHNIQUE_PAYOFFS = {
    "fixed_tp1": lambda p: _target_payoff(p, 0),
    "fixed_tp2": lambda p: _target_payoff(p, 1),
    "fixed_tp3": lambda p: _target_payoff(p, 2),
    "scale_50_50_tp1_tp2": lambda p: _weighted_payoff(p, (.5, .5, 0), False),
    "scale_50_25_25": lambda p: _weighted_payoff(p, (.5, .25, .25), False),
    "scale_33_33_34": lambda p: _weighted_payoff(p, (.33, .33, .34), False),
    "be_after_tp1_50_25_25": lambda p: _weighted_payoff(p, (.5, .25, .25), True),
    "be_after_tp1_33_33_34": lambda p: _weighted_payoff(
        p, (.33, .33, .34), True, step_after_tp2=True,
    ),
}


def payoff_for(technique: str):
    """The payoff function for a technique name.

    `regime_adaptive` is excluded on purpose: its target depends on parameters
    fitted to the train split, so it cannot be evaluated without that context.
    """
    if technique not in TECHNIQUE_PAYOFFS:
        raise KeyError(
            f"{technique!r} has no context-free payoff; known: "
            f"{', '.join(sorted(TECHNIQUE_PAYOFFS))}")
    return TECHNIQUE_PAYOFFS[technique]


def _summary(payoffs: list[float]) -> dict:
    if not payoffs:
        return {"trades": 0, "wins": 0, "losses": 0, "flat": 0,
                "win_rate": None, "net_r": 0.0, "expectancy_r": None,
                "profit_factor": None, "max_drawdown_r": 0.0}
    wins = sum(value > 0 for value in payoffs)
    losses = sum(value < 0 for value in payoffs)
    positives = sum(value for value in payoffs if value > 0)
    negatives = -sum(value for value in payoffs if value < 0)
    equity = peak = drawdown = 0.0
    for value in payoffs:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": len(payoffs), "wins": wins, "losses": losses,
        "flat": len(payoffs) - wins - losses,
        "win_rate": round(wins / len(payoffs) * 100, 2),
        "net_r": round(sum(payoffs), 3),
        "expectancy_r": round(sum(payoffs) / len(payoffs), 4),
        "profit_factor": round(positives / negatives, 3) if negatives else None,
        "max_drawdown_r": round(drawdown, 3),
    }


def _split(plans: list[dict], bars: int) -> dict[str, list[dict]]:
    cut1, cut2 = int(bars * 0.60), int(bars * 0.80)
    return {
        "train": [p for p in plans if p["signal_index"] < cut1 - PURGE_BARS],
        "validation": [p for p in plans if cut1 <= p["signal_index"] < cut2 - PURGE_BARS],
        "holdout": [p for p in plans if p["signal_index"] >= cut2],
    }


def _regime_target(data: pd.DataFrame, plan: dict, params: tuple[int, int, int, int]) -> int:
    strong_score, strong_adx, mid_score, mid_adx = params
    row = data.iloc[plan["signal_index"]]
    direction = plan["direction"]
    score = int(row["long_score"] if direction == 1 else row["short_score"])
    htf = bool(row["htf_bull"] if direction == 1 else row["htf_bear"])
    adx = float(row["adx"])
    if htf and score >= strong_score and adx >= strong_adx:
        return 2
    if score >= mid_score and adx >= mid_adx:
        return 1
    return 0


def evaluate(data: pd.DataFrame, result: dict, symbol: str, timeframe: str) -> dict:
    features = result["data"]
    plans = [p for p in result["plans"] if p["entry_fill_index"] is not None]
    groups = _split(plans, len(data))
    techniques = dict(TECHNIQUE_PAYOFFS)

    grid = list(itertools.product((7, 8), (20, 25, 30), (6, 7), (12, 16, 20)))
    def regime_values(plans_subset: list[dict], params) -> list[float]:
        return [_target_payoff(plan, _regime_target(features, plan, params)) -
                _cost_r(data, plan, symbol) for plan in plans_subset]
    ranked = sorted(
        ((sum(regime_values(groups["train"], params)), params) for params in grid),
        reverse=True,
    )
    chosen_params = ranked[0][1]

    results = {}
    for name, payoff in techniques.items():
        results[name] = {}
        for split_name, split_plans in groups.items():
            values = [payoff(plan) - _cost_r(data, plan, symbol) for plan in split_plans]
            results[name][split_name] = _summary(values)
    results["regime_adaptive"] = {
        split_name: _summary(regime_values(split_plans, chosen_params))
        for split_name, split_plans in groups.items()
    }

    holdout_ranking = sorted(
        ({"technique": name, **parts["holdout"]} for name, parts in results.items()),
        key=lambda row: row["net_r"], reverse=True,
    )
    chart_techniques = {
        "TP1 ทั้งก้อน": techniques["fixed_tp1"],
        "TP3 ทั้งก้อน": techniques["fixed_tp3"],
        "แบ่ง 33/33/34 + BE + step": techniques["be_after_tp1_33_33_34"],
    }
    chart = {"time": [str(pd.Timestamp(data["time"].iloc[p["signal_index"]]))
                       for p in groups["holdout"]], "equity": {}, "drawdown": {}}
    for label, payoff in chart_techniques.items():
        equity_values, drawdown_values = [], []
        equity = peak = 0.0
        for plan in groups["holdout"]:
            equity += payoff(plan) - _cost_r(data, plan, symbol)
            peak = max(peak, equity)
            equity_values.append(round(equity, 4))
            drawdown_values.append(round(equity - peak, 4))
        chart["equity"][label] = equity_values
        chart["drawdown"][label] = drawdown_values
    all_curve = {"time": chart["time"], "equity": {}, "drawdown": {}}
    all_payoffs = dict(techniques)
    all_payoffs["regime_adaptive"] = (
        lambda plan: _target_payoff(plan, _regime_target(features, plan, chosen_params))
    )
    for name, payoff in all_payoffs.items():
        equity_values, drawdown_values = [], []
        equity = peak = 0.0
        for plan in groups["holdout"]:
            equity += payoff(plan) - _cost_r(data, plan, symbol)
            peak = max(peak, equity)
            equity_values.append(round(equity, 4))
            drawdown_values.append(round(equity - peak, 4))
        all_curve["equity"][name] = equity_values
        all_curve["drawdown"][name] = drawdown_values
    return {
        "symbol": symbol.upper(), "timeframe": timeframe.upper(), "bars": len(data),
        "period": {"from": str(pd.Timestamp(data["time"].iloc[0])),
                   "to": str(pd.Timestamp(data["time"].iloc[-1]))},
        "method": "60% train / 20% validation / 20% locked chronological holdout; 141-bar purge",
        "split_trade_counts": {name: len(items) for name, items in groups.items()},
        "regime_parameters_selected_on_train": {
            "strong_score": chosen_params[0], "strong_adx": chosen_params[1],
            "mid_score": chosen_params[2], "mid_adx": chosen_params[3],
        },
        "techniques": results, "holdout_ranking": holdout_ranking,
        "holdout_curve_r": chart,
        "holdout_all_curves_r": all_curve,
        "notes": [
            "All techniques use the same filled entries; order frequency is unchanged.",
            "Spread, estimated commission, and expected slippage are included in every trade.",
            "Each fixed TP result represents a separate full-position exit policy.",
            "The production split exit banks TP1, moves survivors to cost-covered BE, "
            "then locks the TP3 leg at TP1 after TP2.",
        ],
    }


def save(report: dict) -> Path:
    config.ensure_dirs()
    path = (
        config.TECHNIQUE_REPORT_DIR
        / report["symbol"].upper()
        / report["timeframe"].upper()
        / "report.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
