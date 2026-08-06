"""Drawdown-responsive position risk.

The production ladder starts fast only while the account is close to its
realised balance high-water mark.  Risk falls as drawdown grows and recovers
only when equity recovers.  The high-water mark itself is durable state; a
restart must never turn a drawdown account back into a fresh 1% account.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .state import BotState


class Settings(Protocol):
    """What the ladder needs from a venue's settings, declared structurally.

    `engine` must not import `bot`, and "only for typing" is not an exemption:
    the import still makes static tooling resolve the venue layer from inside
    the shared one, and it is the first step of a real dependency. A Protocol
    says the same thing about the shape without naming the class.
    """

    dynamic_risk_enabled: bool


@dataclass(frozen=True)
class RiskDecision:
    risk_percent: float
    drawdown_percent: float
    high_water_balance: float


def decide(settings: "Settings", state: "BotState", equity: float) -> RiskDecision:
    """Return the risk tier for a new setup at ``equity``.

    Drawdown is measured against the highest *closed* balance observed on the
    account, while current equity supplies the low side.  Floating losses thus
    reduce risk immediately, but a temporary floating profit cannot ratchet the
    high-water mark upward and leave the account throttled after it disappears.
    Percentages use initial capital, matching every FTMO loss limit and sizing
    calculation in the bot.
    """
    initial = float(state.initial_balance or settings.initial_balance or 0.0)
    high_water = max(float(state.balance_high_water or 0.0), initial)
    drawdown = (max(0.0, high_water - float(equity)) / initial * 100.0
                if initial > 0 else 0.0)
    if not settings.dynamic_risk_enabled:
        risk = settings.risk_percent
    elif drawdown < settings.dynamic_risk_dd1_percent:
        risk = settings.dynamic_risk_max_percent
    elif drawdown < settings.dynamic_risk_dd2_percent:
        risk = settings.dynamic_risk_tier2_percent
    elif drawdown < settings.dynamic_risk_dd3_percent:
        risk = settings.dynamic_risk_tier3_percent
    else:
        risk = settings.risk_percent
    return RiskDecision(float(risk), float(drawdown), high_water)


def decide_dollars(settings: "Settings", state: "BotState",
                   equity: float) -> RiskDecision:
    """The same ladder as `decide`, denominated in dollars.

    Identical rule, different unit -- which is the whole difference between the
    two venues. FTMO's limits are percentages of initial capital so the forex
    ladder is in percent; TopStep's are fixed dollar amounts that do not scale
    with equity, so expressing the same tiers as percentages of a moving balance
    would make the ladder drift against the rule that ends the account.

    `risk_percent` on the returned decision carries dollars. The field keeps its
    name so the callers, journal keys and tests read the same at both venues.
    """
    initial = float(state.initial_balance or settings.initial_balance or 0.0)
    high_water = max(float(state.balance_high_water or 0.0), initial)
    drawdown = max(0.0, high_water - float(equity))
    if not settings.dynamic_risk_enabled:
        return RiskDecision(float(settings.risk_dollars), float(drawdown), high_water)
    for threshold, size in ladder_steps(settings):
        if drawdown < threshold:
            return RiskDecision(float(size), float(drawdown), high_water)
    return RiskDecision(float(settings.risk_dollars), float(drawdown), high_water)


def ladder_steps(settings: "Settings") -> tuple[tuple[float, float], ...]:
    """(drawdown threshold, risk) pairs, shallowest first.

    The configured three steps land on `risk_dollars`, and the ladder used to
    stop there -- an account $1,000 down went on requesting the same size as an
    account $760 down, right where the trailing floor is closest. The recovery
    tiers continue the same pattern at the same spacing, so a deep drawdown
    keeps shrinking the trade instead of holding it flat.

    Measured on the flat regime: continuing the ladder is what turns 5% of
    paths from "alive but permanently stuck" into passes.
    """
    step = settings.dynamic_risk_dd2_dollars - settings.dynamic_risk_dd1_dollars
    steps = [(settings.dynamic_risk_dd1_dollars, settings.dynamic_risk_max_dollars),
             (settings.dynamic_risk_dd2_dollars, settings.dynamic_risk_tier2_dollars),
             (settings.dynamic_risk_dd3_dollars, settings.dynamic_risk_tier3_dollars)]
    threshold = settings.dynamic_risk_dd3_dollars
    continuation = (settings.risk_dollars,
                    *getattr(settings, "recovery_risk_dollars", ()))
    for size in continuation:
        threshold += step
        steps.append((threshold, size))
    # Preserve the configured boundary for the final recovery tier, then make
    # that same tier terminal. Without this sentinel, drawdown exactly at the
    # last finite threshold falls through to the defensive floor risk.
    steps.append((float("inf"), continuation[-1]))
    return tuple(steps)


def fitting_tiers(settings: "Settings", ceiling: float) -> tuple[float, ...]:
    """Configured dollar risk tiers at or below ``ceiling``, largest first.

    This was copied from the forex tree unchanged and still read
    `dynamic_risk_max_percent` and `risk_percent`, neither of which exists on
    the futures Settings -- so the one helper meant to fit a trade into the
    room left raised AttributeError the first time anything called it.
    """
    values = (settings.dynamic_risk_max_dollars,
              settings.dynamic_risk_tier2_dollars,
              settings.dynamic_risk_tier3_dollars,
              settings.risk_dollars,
              *getattr(settings, "recovery_risk_dollars", ()))
    return tuple(sorted({float(value) for value in values
                         if value <= ceiling + 1e-12}, reverse=True))


def fit_to_room(settings: "Settings", requested: float, room: float) -> float:
    """The largest configured tier that fits in ``room``, or 0.0 if none does.

    Never invents a size and never rounds up: it picks from the tiers the
    operator already approved. Returning 0.0 means the honest answer is no
    trade, which is the only safe answer when the account has less room left
    than its smallest configured risk.
    """
    if room <= 0:
        return 0.0
    if requested <= room:
        return float(requested)
    if not settings.dynamic_risk_fit_remaining:
        return 0.0
    tiers = fitting_tiers(settings, min(requested, room))
    return float(tiers[0]) if tiers else 0.0
