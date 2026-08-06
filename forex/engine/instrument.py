"""What sizing needs to know about a tradeable instrument, at any venue.

This used to be `broker.SymbolSpec`, defined next to the MT5 session that built
it, which made `sizing` import MetaTrader-shaped fields to divide two numbers.
Futures do not have lots: TopStep fills whole contracts, `volume_step` is 1, and
the minimum is 1 contract rather than 0.01 of one. The field names below are the
lot vocabulary because the forex instance and its tests are written in it; a
futures adapter maps its tick size and tick value onto the same three questions
sizing actually asks -- how small a unit, how many of them, and what one point
of price is worth.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InstrumentSpec:
    name: str                # venue's own name, e.g. XAUUSDm or MGCZ26
    digits: int
    point: float
    volume_min: float        # futures: 1 contract
    volume_max: float
    volume_step: float       # futures: 1 -- fractional contracts do not exist
    value_per_point: float   # account currency per 1.0 price point, per 1.0 unit
    stops_level_points: float
    filling: int             # MT5 filling mode; futures adapters pass 0


# The forex instance and its tests still say SymbolSpec. Keeping the old name as
# an alias means this rename cannot be the reason a live order stops going out.
SymbolSpec = InstrumentSpec
