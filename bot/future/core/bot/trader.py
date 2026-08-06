"""Turning a plan into real futures orders, counted in contracts.

The forex trader divides a lot: 0.03 splits into 0.01/0.01/0.01 and, when it
will not divide, `floor_to_step` quietly gives back a slightly smaller position.
Contracts do not work like that. There is no 0.4 of an MNQ, the split exit needs
three whole contracts before it can exist at all, and rounding "just this once"
is how a $200 risk becomes a $340 one.

So every function here is written around three rules:

1.  Size down, never up. A position that does not reach one contract is a trade
    that does not get taken.
2.  The three-leg exit requires three contracts. Below that the trade runs the
    single-leg fixed-TP3 exit -- the same policy the lab measured -- rather than
    a two-leg approximation of it that was never tested.
3.  Real risk is what the rounded size actually risks, not what was requested.
    Exposure checks are given the real number.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from engine.signals import Intent

from .broker import (Broker, OrderRejected, ProjectXError, risk_dollars_for,
                     size_contracts)
from .settings import Settings


@dataclass(frozen=True)
class ContractPlan:
    """What will actually be sent, after contracts have had their say."""
    contracts: int
    legs: tuple[int, ...]              # contracts per leg, sums to `contracts`
    exit_mode: str                     # fixed_tp3 | be_33_33_34
    stop_distance_points: float
    risk_dollars: float                # real risk of the rounded size
    requested_risk_dollars: float

    @property
    def single_leg(self) -> bool:
        return len(self.legs) == 1

    @property
    def rounding_gave_up(self) -> float:
        """Risk dropped by rounding down. Positive means the trade is smaller."""
        return self.requested_risk_dollars - self.risk_dollars


def split_contracts(total: int, weights: tuple[float, ...]) -> tuple[int, ...]:
    """Divide whole contracts across legs as close to `weights` as integers allow.

    Largest remainder, not truncation. Truncating each leg and dumping what is
    left on the runner looks harmless at three contracts and is badly wrong
    above it: `int(6 * 0.33)` is 1, so six contracts came out 1/1/4 -- a 17/17/66
    split pretending to be 33/33/34. That is a different exit from the one the
    lab measured, with two thirds of the position riding to TP3 instead of one.

    3 contracts is 1/1/1, 6 is 2/2/2, and where a remainder genuinely exists it
    goes to the leg with the largest fractional claim, ties to the runner --
    which keeps the tail of the distribution rather than the part that exits
    first.
    """
    if total < len(weights):
        raise ValueError(f"{total} contracts cannot fill {len(weights)} legs")
    exact = [total * weight for weight in weights]
    legs = [max(1, int(value)) for value in exact]
    remaining = total - sum(legs)
    # Hand out what integer floors left over, largest fractional part first;
    # reversed() breaks ties toward the runner.
    order = sorted(range(len(weights)),
                   key=lambda i: (exact[i] - int(exact[i]), i), reverse=True)
    while remaining > 0:
        for index in order:
            if remaining <= 0:
                break
            legs[index] += 1
            remaining -= 1
    while remaining < 0:                # min-1 floors overspent the total
        for index in reversed(order):
            if remaining >= 0:
                break
            if legs[index] > 1:
                legs[index] -= 1
                remaining += 1
    return tuple(legs)


def plan_contracts(settings: Settings, entry: float, stop: float,
                   risk_dollars: float | None = None) -> ContractPlan:
    """Size a trade in whole contracts and pick the exit its size can support."""
    requested = float(settings.risk_dollars if risk_dollars is None else risk_dollars)
    distance = abs(float(entry) - float(stop))
    if distance <= 0:
        raise OrderRejected("entry and stop are the same price; no risk to size")

    contracts = size_contracts(settings, requested, distance)
    if contracts < 1:
        per_contract = distance * settings.value_per_point
        raise OrderRejected(
            f"${requested:,.0f} of risk does not reach one contract: the stop is "
            f"{distance:,.2f} points, which costs ${per_contract:,.2f} per contract")

    exit_mode = settings.resolved_exit_mode(contracts)
    weights = settings.leg_weights_for(contracts)
    legs = (contracts,) if exit_mode == "fixed_tp3" else split_contracts(contracts, weights)
    return ContractPlan(
        contracts=contracts,
        legs=legs,
        exit_mode=exit_mode,
        stop_distance_points=distance,
        risk_dollars=risk_dollars_for(settings, contracts, distance),
        requested_risk_dollars=requested,
    )


@dataclass
class OpenResult:
    """What the gateway has been asked to do, and what it confirmed.

    `accepted_contracts` is deliberately not called "filled". A market order the
    gateway acknowledged is an order that EXISTS; whether it filled, filled
    partially, or is still working is a question only the order/position read
    APIs can answer. The previous version incremented a single `sent_contracts`
    counter straight from the request and treated acceptance as a full fill,
    which is how a position ends up sized differently from the one being
    managed. Until the reconciliation loop exists, callers must treat
    `needs_reconciliation` as "a human or the run loop must look at the
    account".
    """
    plan: ContractPlan
    responses: list[dict] = field(default_factory=list)
    order_ids: list[object] = field(default_factory=list)
    accepted_contracts: int = 0
    ambiguous: bool = False
    error: str = ""

    @property
    def sent_contracts(self) -> int:
        """Compatibility alias. Acceptance, never a fill confirmation."""
        return self.accepted_contracts

    @property
    def opened(self) -> bool:
        return self.accepted_contracts > 0

    @property
    def partial(self) -> bool:
        """Some legs went out and some did not -- the state that needs a human."""
        return 0 < self.accepted_contracts < self.plan.contracts

    @property
    def needs_reconciliation(self) -> bool:
        """True when live exposure may not match what this object records.

        An ambiguous write -- a timeout, a dropped connection, an HTTP error
        after the request left -- is NOT evidence the order was refused. It has
        to be resolved by reading the account back.
        """
        return self.ambiguous or self.partial


def targets_for(intent: Intent, exit_mode: str) -> tuple[float, ...]:
    """The take-profit each leg is sent with.

    `fixed_tp3` runs one leg to the furthest target, which is what the lab
    measured for a position too small to split. The three-leg exit sends one leg
    to each target; the stop is moved later, by `stop_after_tp1`.
    """
    if exit_mode == "fixed_tp3":
        return (intent.targets[-1],)
    return tuple(intent.targets)


def open_trade(broker: Broker, settings: Settings, intent: Intent,
               risk_dollars: float | None = None) -> OpenResult:
    """Send the entry as one order per leg, each with its own stop and target.

    Legs go out one at a time and every accepted order id is recorded BEFORE the
    next write, so a failure midway leaves a record of what is already live.

    Every ambiguous outcome is caught, not just a clean rejection. This used to
    catch `OrderRejected` alone while the real broker raises `ProjectXError` for
    HTTP errors, network failures and gateway-level refusals -- so leg one being
    accepted and leg two timing out let the exception escape with no result
    object at all, and the caller never learned that contracts were live.

    A timeout is never treated as "did not happen".
    """
    plan = plan_contracts(settings, intent.entry, intent.stop, risk_dollars)
    targets = targets_for(intent, plan.exit_mode)
    result = OpenResult(plan=plan)
    for contracts, target in zip(plan.legs, targets):
        try:
            response = broker.place_market(intent.direction, contracts,
                                           intent.stop, target)
        except OrderRejected as error:
            # An explicit refusal: nothing was accepted for THIS leg.
            result.error = str(error)
            if result.opened:
                return result           # partial: caller must reconcile
            raise
        except ProjectXError as error:
            # Ambiguous: the request may have reached the gateway. Whether or
            # not earlier legs went out, the account has to be read back.
            result.error = str(error)
            result.ambiguous = True
            return result
        result.responses.append(response)
        if isinstance(response, dict) and response.get("orderId") is not None:
            result.order_ids.append(response["orderId"])
        elif not (isinstance(response, dict) and response.get("dry_run")):
            # Accepted, but the gateway named no order id -- there is nothing to
            # reconcile against later, which is itself a reason to look.
            result.ambiguous = True
        result.accepted_contracts += contracts
    return result


def cost_points(settings: Settings) -> float:
    """Price distance a leg must clear before breakeven really is breakeven.

    Commission and the expected exit slip, converted to price. Passing 0.0 --
    which is what every call site did while no cost model existed -- produces a
    stop at the raw fill, which still loses money once the account is charged.
    """
    return settings.cost_points


def _validated_cost_points(value: float) -> float:
    """Reject an omitted/invalid cost instead of silently using raw breakeven."""
    cost = float(value)
    if not math.isfinite(cost) or cost < 0:
        raise ValueError("cost_points must be a finite non-negative value")
    return cost


def stop_after_tp1(intent: Intent, fill_price: float,
                   cost_points: float) -> float:
    """Where the stop goes once the first leg is closed.

    Breakeven means the trade cannot lose, which is not the same as the entry
    price: the remaining legs still owe commission and whatever the exit slips.
    So breakeven is the actual fill plus those costs, in the direction of the
    trade -- the same correction the forex trader applies for commission,
    slippage and swap.
    """
    cost = _validated_cost_points(cost_points)
    return (fill_price + cost if intent.direction > 0
            else fill_price - cost)


def stop_after_tp2(intent: Intent, cost_points: float) -> float:
    """Where the last leg's stop goes once TP2 is banked.

    The second step of the same three-leg technique the forex instance runs: the
    survivor does not sit at breakeven for the rest of the trade, it steps up to
    lock the TP1 level. Cost is added the same way -- the lock is only a lock if
    it still covers commission and the slip on the way out.

    This is the step that turns the exit from `be_after_tp1_33_33_34` into the
    version the lab actually measured (+0.357R on M30 holdout). Leaving it out
    would mean the futures account runs a different system from the one the
    simulation priced, which is the exact drift both venues exist to avoid.
    """
    target = intent.targets[0]
    cost = _validated_cost_points(cost_points)
    return (target + cost if intent.direction > 0
            else target - cost)


def risk_for(settings: Settings, state, equity: float,
             room: float | None = None) -> float:
    """Dollars of risk for the next setup, after the ladder and the room left.

    Same ladder, same thresholds and the same durable high-water mark as the
    forex instance -- only the unit differs. When `room` is given, the tier is
    additionally fitted into the distance remaining above the nearest loss
    floor, and 0.0 means no configured tier fits: no trade.
    """
    from engine.dynamic_risk import decide_dollars, fit_to_room

    tier = decide_dollars(settings, state, equity).risk_percent
    if room is None:
        return tier
    return fit_to_room(settings, tier, float(room))


def open_risk_dollars(settings: Settings, positions, stop_price: float) -> float:
    """Dollars currently at risk across open contracts, at their shared stop."""
    total = 0.0
    for position in positions:
        distance = abs(position.price_open - stop_price)
        total += risk_dollars_for(settings, position.contracts, distance)
    return total
