"""Durable bot state.

Restarting the process must not reset the daily loss counter or forget that a
plan is already working — otherwise a crash loop becomes a way to trade past the
guardrails. Everything the guards need therefore lives on disk, written after
every change.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from datetime import date
from pathlib import Path


@dataclass
class ManagedTrade:
    """One plan and the broker tickets it turned into."""
    plan_id: str                       # "<timeframe>@<signal bar time>"
    timeframe: str
    direction: int
    entry: float
    stop: float
    risk: float                        # stop distance in price units = 1R
    risk_cash: float                   # what 1R is worth, needed to score in R
    targets: list[float]
    legs: list[float]
    position_tickets: list[int] = field(default_factory=list)
    pending_tickets: list[int] = field(default_factory=list)
    filled_at: str | None = None
    fill_bar_time: str | None = None
    breakeven_done: bool = False
    closed: bool = False
    dry_run: bool = False              # simulated: never scored against real deals


@dataclass
class BotState:
    initial_balance: float = 0.0
    # Last offset measured from a live tick. Kept because it cannot be measured
    # while the market is closed, and the news calendar needs it to line up.
    server_utc_offset: float | None = None
    day_key: str = ""                  # broker-server calendar day
    day_start_balance: float = 0.0
    # FTMO measures the daily loss against the higher of balance and equity at
    # the day's open, so both are recorded; see `roll_day`.
    day_start_equity: float = 0.0
    day_realised: float = 0.0          # cash, this trading day
    # Terminal requests already spent today, carried across restarts so a crash
    # loop cannot quietly reset the count and blow the EA allowance.
    day_requests: int = 0
    consecutive_losses: int = 0
    halted_forever: bool = False       # max loss breached: never trade again
    halted_reason: str = ""
    paused_until_day: str = ""         # daily stop hit: resume next server day
    # Server days on which at least one position was opened. FTMO's 2-Step needs
    # four of them, so it has to survive restarts like everything else here.
    trading_days: list[str] = field(default_factory=list)
    seen_plan_ids: list[str] = field(default_factory=list)
    trades: dict[str, ManagedTrade] = field(default_factory=dict)

    # -- persistence ------------------------------------------------------
    @classmethod
    def load(cls, path: Path) -> "BotState":
        """Read state, ignoring fields this version no longer knows about.

        A live bot must not die because an older or newer `state.json` carries an
        extra key — a crash on startup with positions open is worse than any
        field being dropped, and dropping one is visible in the journal.
        """
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        trade_fields = {spec.name for spec in fields(ManagedTrade)}
        trades = {
            key: ManagedTrade(**{name: value for name, value in payload.items()
                                 if name in trade_fields})
            for key, payload in raw.pop("trades", {}).items()}
        known = {spec.name for spec in fields(cls)} - {"trades"}
        return cls(**{key: value for key, value in raw.items() if key in known},
                   trades=trades)

    def save(self, path: Path) -> None:
        payload = asdict(self)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # -- day handling -----------------------------------------------------
    def roll_day(self, server_day: date, balance: float,
                 equity: float | None = None) -> bool:
        """Start a new trading day when the broker clock passes midnight.

        `day_start_equity` keeps the *higher* of balance and equity because that
        is the reference FTMO measures the daily loss against. Holding a winning
        position overnight lifts equity above balance, and using balance alone
        would leave the bot believing it has more daily room than the rules give
        it.

        The loss streak resets here too. It is a "stand down for the rest of the
        day" rule, and it can only be cleared by a winning trade — so without
        this reset a day that ends on the third loss would pause the bot for
        every day after it as well, with no trade left that could ever clear it.
        """
        key = server_day.isoformat()
        if key == self.day_key:
            return False
        self.day_key = key
        self.day_start_balance = balance
        self.day_start_equity = max(balance, equity if equity is not None else balance)
        self.day_realised = 0.0
        self.day_requests = 0
        self.consecutive_losses = 0
        return True

    @property
    def is_paused_today(self) -> bool:
        return self.paused_until_day == self.day_key

    def pause_today(self) -> None:
        self.paused_until_day = self.day_key

    def halt(self, reason: str) -> None:
        self.halted_forever = True
        self.halted_reason = reason

    def count_trading_day(self) -> None:
        """Record that this server day now has a position on it."""
        if self.day_key and self.day_key not in self.trading_days:
            self.trading_days.append(self.day_key)

    def remember_plan(self, plan_id: str, keep: int = 400) -> None:
        self.seen_plan_ids.append(plan_id)
        del self.seen_plan_ids[:-keep]

    def open_trades(self) -> list[ManagedTrade]:
        return [trade for trade in self.trades.values() if not trade.closed]
