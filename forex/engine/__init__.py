"""Venue-agnostic execution machinery for this tree.

Nothing in here may import `bot`: the dependency runs one way, `bot` -> `engine`,
so a change made for the venue cannot reach the arithmetic underneath it.

This tree is one venue. The other venue has its own copy of this package, which
means a fix here is NOT a fix there -- that is the price of keeping `forex/` and
`future/` from ever sharing a line of code, and it has to be paid deliberately,
by applying the change twice.

What belongs here is anything the venue cannot change the meaning of: the
instrument contract, sizing arithmetic, the drawdown ladder, durable state, the
journal, the single-instance lock, session windows, and the news calendar. What
belongs in `bot` is the broker session, the prop firm's rules, and the settings
that name an account.
"""
from __future__ import annotations

from .instrument import InstrumentSpec

__all__ = ["InstrumentSpec"]
