"""Position sizing: turn "risk 0.40% of the account" into broker lots.

The backtest measures everything in R, where 1R is the distance from entry to
stop. Live, that only holds if every trade risks the same cash amount, so lot
size has to be derived from the stop distance on every single trade — never
fixed, never scaled up after a loss.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .broker import SymbolSpec


class SizingError(RuntimeError):
    """Raised when a plan cannot be sized within the broker's own limits."""


@dataclass(frozen=True)
class Sizing:
    total_lots: float
    legs: tuple[float, ...]          # one entry per take-profit level used
    #: Cash the position can actually lose — lots that will really be sent times
    #: the risk each lot carries. This is the denominator R must be measured
    #: against. It used to hold the *intended* budget instead, which is a
    #: different number whenever the lot step rounds the size down: at 10,000 and
    #: 0.40% the intent is 40.00 but 0.01 lot of gold only risks 20.96, so a full
    #: stop-out read as -0.52R and every live expectancy came out ~1.9x too kind.
    risk_cash: float
    #: What the settings asked for, kept so the gap stays visible.
    intended_risk_cash: float
    risk_per_lot: float
    single_leg: bool                 # True when size could not split three ways

    @property
    def risk_shortfall(self) -> float:
        """Fraction of the intended risk the rounded size actually carries."""
        return (self.risk_cash / self.intended_risk_cash
                if self.intended_risk_cash else 1.0)


def floor_to_step(volume: float, step: float) -> float:
    if step <= 0:
        return volume
    # Round the ratio first: 0.03/0.01 lands on 2.9999996 in binary floating point.
    return math.floor(round(volume / step, 8)) * step


def nearest_step(volume: float, step: float) -> float:
    if step <= 0:
        return volume
    return round(round(volume / step, 8)) * step


def size_plan(spec: SymbolSpec, balance: float, risk_percent: float,
              stop_distance: float, weights=(0.33, 0.33, 0.34),
              rounding: str = "down", max_overshoot: float = 0.15) -> Sizing:
    """Lots for one plan, split across the take-profit legs.

    `stop_distance` is in price units (the plan's own `risk` field).

    `rounding` decides what happens to the fraction of a lot step that the
    account is entitled to but cannot be expressed:

      down     always floor. Never risks more than asked, but on a small balance
               throws away most of the budget — 0.40% of 10,000 wants 0.0191 lots
               of gold and gets 0.01, which is 0.21%.
      nearest  round to the closest step, but only while the result stays within
               `max_overshoot` of the intended risk; otherwise floor.

    The cap is what makes `nearest` safe. Rounding up is nearly free when the
    fraction is small relative to the position — at 10,000 it turns 0.21% into
    0.42%, over target by 5% — and reckless when it is not: at 2,700 the same
    rule would turn 0.40% into 0.78%, because half a step is most of the trade.
    """
    if stop_distance <= 0:
        raise SizingError("stop distance must be positive")
    # A stop closer than the broker's own minimum is rejected at `order_send`, and
    # finding that out mid-way through placing three legs is exactly the failure
    # that leaves part of a trade live. Refuse the plan here instead.
    min_distance = spec.stops_level_points * spec.point
    if min_distance > 0 and stop_distance < min_distance:
        raise SizingError(
            f"stop is {stop_distance:.5f} but {spec.name} requires at least "
            f"{min_distance:.5f} ({spec.stops_level_points:.0f} points)")
    intended = balance * risk_percent / 100.0
    risk_per_lot = stop_distance * spec.value_per_point
    if risk_per_lot <= 0:
        raise SizingError(f"{spec.name} reports a non-positive value per point")

    raw = intended / risk_per_lot
    capped = min(raw, spec.volume_max)
    total = floor_to_step(capped, spec.volume_step)
    if rounding == "nearest":
        candidate = min(nearest_step(capped, spec.volume_step), spec.volume_max)
        if candidate * risk_per_lot <= intended * (1 + max_overshoot) + 1e-9:
            total = candidate
    if total < spec.volume_min:
        raise SizingError(
            f"risk {risk_percent:.2f}% of {balance:.2f} allows {raw:.4f} lots but "
            f"{spec.name} needs at least {spec.volume_min}. Lower the stop distance "
            f"or raise the account size; do not raise the risk to fit.")

    legs = _split(total, spec, weights)
    # Price the legs that will really be sent, not the ones asked for. `_split`
    # can return a total that differs from `total` when the weights do not divide
    # evenly, and rounding down to the lot step almost always loses something.
    sent = round(sum(legs), 8)
    return Sizing(total_lots=sent, legs=legs,
                  risk_cash=round(sent * risk_per_lot, 6),
                  intended_risk_cash=intended,
                  risk_per_lot=risk_per_lot, single_leg=len(legs) == 1)


def _split(total: float, spec: SymbolSpec, weights) -> tuple[float, ...]:
    """Split into weighted legs, or return one leg when the size is too small.

    Allocation runs in whole lot steps using largest-remainder apportionment.
    Flooring each leg and dumping the remainder on the last one looks simpler but
    skews the weights badly at small sizes — 0.09 lots would come out
    0.02/0.02/0.05, which is a 22/22/56 exit policy, not the 33/33/34 that was
    measured. A leg below the broker minimum cannot be sent at all, and dropping
    one silently would change the policy too, so that case returns one honest leg
    instead of three broken ones.
    """
    step = spec.volume_step or total
    min_units = max(1, round(spec.volume_min / step))
    units = round(total / step)
    if units < min_units * len(weights):
        return (round(total, 8),)

    ideal = [units * weight for weight in weights]
    counts = [max(min_units, int(value)) for value in ideal]
    leftover = units - sum(counts)
    if leftover < 0:
        return (round(total, 8),)
    # Hand out the remaining steps to whichever legs were rounded down hardest.
    order = sorted(range(len(weights)), key=lambda i: ideal[i] - int(ideal[i]), reverse=True)
    for position in range(leftover):
        counts[order[position % len(order)]] += 1
    return tuple(round(count * step, 8) for count in counts)


def open_risk_percent(spec: SymbolSpec, positions, balance: float) -> float:
    """Risk still on the table, as a percentage of balance.

    A leg whose stop already sits at or beyond entry contributes nothing, which
    is exactly what the break-even rule is for.
    """
    if balance <= 0:
        return 0.0
    total = 0.0
    for position in positions:
        if position.stop == 0:
            # No stop attached is an unbounded loss; treat it as the full budget
            # so the guardrails refuse to add more.
            return float("inf")
        distance = ((position.price_open - position.stop) * position.direction)
        if distance <= 0:
            continue
        total += distance * spec.value_per_point * position.volume
    return total / balance * 100.0
