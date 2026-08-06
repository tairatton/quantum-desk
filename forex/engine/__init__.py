"""Venue-agnostic execution machinery shared by every bot instance.

Nothing in here may import a venue package. `bot.forex` (FTMO on MT5) and
`bot.futures` (TopStep on ProjectX) both import *from* core; core never imports
back. That direction is what keeps a change made for futures from silently
altering how the live forex account trades.

What belongs here is anything the venue cannot change the meaning of: the
instrument contract, sizing arithmetic, the drawdown ladder, durable state, the
journal, the single-instance lock, session windows, and the news calendar. What
belongs in a venue package is the broker session, the prop firm's rules, and the
settings that name an account.
"""
from __future__ import annotations

from .instrument import InstrumentSpec

__all__ = ["InstrumentSpec"]
