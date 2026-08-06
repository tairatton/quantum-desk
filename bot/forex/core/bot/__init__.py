"""Live execution layer for the HTF Quantum Adaptive plan.

The strategy itself lives in `strategy.quantum` and is imported unchanged, so the bot
cannot drift away from what the technique lab measured. This package only adds
what live trading needs: a broker session, position sizing, FTMO guardrails,
durable state and a journal.

Dry-run is the default. Nothing here sends an order without `--live`.
"""
from __future__ import annotations

from .settings import Settings, load

__all__ = ["Settings", "load"]
