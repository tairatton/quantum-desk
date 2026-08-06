"""Drawdown-responsive position risk.

The production ladder starts fast only while the account is close to its
realised balance high-water mark.  Risk falls as drawdown grows and recovers
only when equity recovers.  The high-water mark itself is durable state; a
restart must never turn a drawdown account back into a fresh 1% account.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.settings import Settings
    from .state import BotState


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
        risk = settings.risk_dollars
    elif drawdown < settings.dynamic_risk_dd1_dollars:
        risk = settings.dynamic_risk_max_dollars
    elif drawdown < settings.dynamic_risk_dd2_dollars:
        risk = settings.dynamic_risk_tier2_dollars
    elif drawdown < settings.dynamic_risk_dd3_dollars:
        risk = settings.dynamic_risk_tier3_dollars
    else:
        risk = settings.risk_dollars
    return RiskDecision(float(risk), float(drawdown), high_water)


def fitting_tiers(settings: "Settings", ceiling: float) -> tuple[float, ...]:
    """Configured risk tiers at or below ``ceiling``, largest first."""
    values = (settings.dynamic_risk_max_percent,
              settings.dynamic_risk_tier2_percent,
              settings.dynamic_risk_tier3_percent,
              settings.risk_percent)
    return tuple(sorted({float(value) for value in values
                         if value <= ceiling + 1e-12}, reverse=True))
