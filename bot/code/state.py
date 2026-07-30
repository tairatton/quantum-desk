"""Durable bot state.

Restarting the process must not reset the daily loss counter or forget that a
plan is already working — otherwise a crash loop becomes a way to trade past the
guardrails. Everything the guards need therefore lives on disk, written after
every change.
"""
from __future__ import annotations

import json
import os
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
    # Market order ids awaiting an authoritative order -> position mapping from
    # deal history. They are saved after each accepted leg so a hard power loss
    # during placement can be recovered without guessing from price/comment.
    market_order_tickets: list[int] = field(default_factory=list)
    tp1_market_order_ticket: int | None = None
    # TP1 identity is persisted separately from list order. Pending fills can
    # become visible to MT5 history out of order, so "first position ticket" is
    # not a safe long-term definition of the TP1 leg.
    tp1_position_ticket: int | None = None
    tp1_pending_ticket: int | None = None
    filled_at: str | None = None
    fill_bar_time: str | None = None
    breakeven_done: bool = False
    closed: bool = False
    dry_run: bool = False              # simulated: never scored against real deals
    # Concrete policy used when this trade was created. Persisting it prevents a
    # settings edit or balance change from rewriting the management contract of
    # an already-open position after restart.
    exit_mode: str = ""


@dataclass
class BotState:
    # A state file belongs to exactly one broker account. Without this binding,
    # switching from a demo to a Challenge could carry the old initial balance,
    # trading days and tickets into the new account.
    account_login: int | None = None
    account_server: str = ""
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
    def disable_persistence(self) -> None:
        """Keep mutations in memory only, used by dry-run execution."""
        self._persistence_enabled = False

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
        trades = {}
        for key, payload in raw.pop("trades", {}).items():
            known_payload = {name: value for name, value in payload.items()
                             if name in trade_fields}
            if not known_payload.get("exit_mode"):
                # State written before capital tiers did not name the concrete
                # policy. The ticket layout is authoritative: fixed TP3 always
                # had one leg; the measured BE policy had three.
                known_payload["exit_mode"] = (
                    "be_33_33_34" if len(known_payload.get("legs", ())) >= 3
                    else "fixed_tp3")
            # A short-lived release could mark a market trade closed when MT5
            # accepted its orders but positions_get() had not exposed the
            # resulting positions yet. An unresolved order id is proof that
            # reconciliation still has work to do, so reopen that record on
            # load and let deal history settle it.
            if (known_payload.get("closed")
                    and known_payload.get("market_order_tickets")):
                known_payload["closed"] = False
            trades[key] = ManagedTrade(**known_payload)
        known = {spec.name for spec in fields(cls)} - {"trades"}
        return cls(**{key: value for key, value in raw.items() if key in known},
                   trades=trades)

    def save(self, path: Path) -> None:
        if not getattr(self, "_persistence_enabled", True):
            return
        payload = asdict(self)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def bind_account(self, account: dict) -> bool:
        """Bind legacy/fresh state to one login, or fail closed on a mismatch.

        Returns True only when the identity was recorded for the first time.
        A legacy state with a materially different initial balance is refused:
        that is the tell-tale case of taking a $10K state into a $50K account.
        """
        login = int(account.get("login") or 0)
        server = str(account.get("server") or "")
        balance = float(account.get("balance") or 0.0)
        if login <= 0 or not server:
            raise ValueError("MT5 account identity is incomplete; refusing LIVE trading")
        if self.account_login is None:
            if self.initial_balance > 0 and balance > 0:
                difference = abs(balance - self.initial_balance) / self.initial_balance
                if difference > 0.25:
                    raise ValueError(
                        f"legacy state is anchored at {self.initial_balance:,.2f}, "
                        f"but MT5 balance is {balance:,.2f}; archive state.json before "
                        "using a different account")
            self.account_login = login
            self.account_server = server
            return True
        if self.account_login != login or self.account_server != server:
            raise ValueError(
                f"state belongs to {self.account_login}@{self.account_server}, "
                f"but MT5 is {login}@{server}; use a separate state.json")
        return False

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
        # A brand-new state has both fields empty; empty == empty must not make
        # the account look paused before the first server day is anchored.
        return bool(self.day_key) and self.paused_until_day == self.day_key

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
