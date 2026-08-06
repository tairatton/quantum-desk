"""Live execution for the FUTURES instance: TopStep, via the ProjectX Gateway.

This package is the futures twin of `bot.forex`. Both import the shared engine
from `engine/` and neither imports the other. The separation is deliberate and
goes all the way down:

    bot/    FTMO      MT5            XAUUSD, sized in lots
    bot/   TopStep   ProjectX API   CME futures, sized in whole contracts
    engine/       shared    -              sizing, state, journal, news, sessions

Each tree keeps its own settings, state file, journal and single-instance lock,
so one can be stopped, repaired or breached without touching the other.
The two prop firms do not share a rulebook -- FTMO's max loss is static and
measured from the initial balance, TopStep's trails the highest end-of-day
balance -- which is exactly why the guardrails live in the venue package rather
than in the engine.

Dry-run is the default here too. Nothing sends an order without `--live`.
"""
from __future__ import annotations

from .settings import Settings, load

__all__ = ["Settings", "load"]
