"""Durable bot state.

Restarting the process must not reset the daily loss counter or forget that a
plan is already working — otherwise a crash loop becomes a way to trade past the
guardrails. Everything the guards need therefore lives on disk, written after
every change.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


_REPLACE_RETRY_DELAYS = (0.02, 0.05, 0.10, 0.20, 0.40, 0.80)
FTMO_TIMEZONE = ZoneInfo("Europe/Prague")


def ftmo_day(server_time: datetime, server_utc_offset: float) -> date:
    """Return the official evaluation day at FTMO's 00:00 CE(S)T boundary."""
    utc = (server_time - timedelta(hours=float(server_utc_offset))).replace(
        tzinfo=timezone.utc)
    return utc.astimezone(FTMO_TIMEZONE).date()


def ftmo_day_start_server(server_time: datetime,
                          server_utc_offset: float) -> datetime:
    """Return the current FTMO midnight expressed in broker-server time.

    Europe/Prague changes between CET and CEST while a broker can use a
    different DST schedule. Converting through UTC keeps the boundary correct
    instead of assuming that broker midnight and FTMO midnight are identical.
    """
    evaluation_day = ftmo_day(server_time, server_utc_offset)
    local_midnight = datetime.combine(
        evaluation_day, datetime.min.time(), tzinfo=FTMO_TIMEZONE,
    )
    utc_midnight = local_midnight.astimezone(timezone.utc).replace(tzinfo=None)
    return utc_midnight + timedelta(hours=float(server_utc_offset))


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
    # Market order/deal ids awaiting an authoritative recovery-id -> position
    # mapping from deal history. They are saved after each accepted leg so a
    # hard power loss during placement can be recovered without guessing from
    # price/comment.
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
    # The working limit was conclusively cancelled for a limit -> market
    # conversion. Keep this durable through the replacement attempt so a guard,
    # first-leg rejection, crash or reconnect cannot retry the same conversion
    # bar.
    conversion_released: bool = False
    # Converted market entries are sized against a deliberately adverse price
    # before a fill exists. Replace that conservative estimate with the real
    # fill-to-stop cash risk once every accepted entry deal is in history. This
    # flag makes the unfinished accounting work survive a restart.
    converted_risk_pending: bool = False
    expected_market_volume: float = 0.0
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
    # Highest closed balance observed on this account. Dynamic risk compares
    # current equity against it, and persistence prevents a restart from
    # resetting a drawdown account to the highest risk tier.
    balance_high_water: float = 0.0
    # Last offset measured from a live tick. Kept because it cannot be measured
    # while the market is closed, and the news calendar needs it to line up.
    server_utc_offset: float | None = None
    day_key: str = ""                  # FTMO Europe/Prague calendar day
    day_start_balance: float = 0.0
    # Retained for diagnostics/backward compatibility. The 2-Step loss floor
    # uses day_start_balance, never the higher opening equity.
    day_start_equity: float = 0.0
    day_realised: float = 0.0          # cash, this trading day
    # Terminal requests already spent today, carried across restarts so a crash
    # loop cannot quietly reset the count and blow the EA allowance.
    day_requests: int = 0
    consecutive_losses: int = 0
    halted_forever: bool = False       # max loss breached: never trade again
    halted_reason: str = ""
    paused_until_day: str = ""         # daily stop hit: resume next FTMO day
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
        # On Windows, indexers, antivirus and editor file watchers can briefly
        # open the destination without FILE_SHARE_DELETE.  MoveFileEx (which
        # backs os.replace) then raises WinError 5 even though the fully-fsynced
        # temporary snapshot is ready.  A short bounded retry preserves the
        # atomic-write contract without letting a transient reader crash the
        # live process immediately after an order was accepted.
        for delay in (*_REPLACE_RETRY_DELAYS, None):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if delay is None:
                    raise
                time.sleep(delay)

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

    def observe_balance(self, balance: float) -> bool:
        """Raise the durable realised-balance high-water mark when appropriate."""
        candidate = max(float(balance or 0.0), float(self.initial_balance or 0.0))
        if candidate <= self.balance_high_water:
            return False
        self.balance_high_water = candidate
        return True

    # -- day handling -----------------------------------------------------
    def roll_day(self, evaluation_day: date, balance: float,
                 equity: float | None = None) -> bool:
        """Start a new trading day at Europe/Prague midnight.

        ``evaluation_day`` must be the Europe/Prague calendar date. FTMO
        2-Step uses balance at 00:00 CE(S)T; floating equity is diagnostic only.

        The loss streak resets here too. It is a "stand down for the rest of the
        day" rule, and it can only be cleared by a winning trade — so without
        this reset a day that ends on the third loss would pause the bot for
        every day after it as well, with no trade left that could ever clear it.
        """
        key = evaluation_day.isoformat()
        if key == self.day_key:
            return False
        self.day_key = key
        self.day_start_balance = balance
        self.day_start_equity = equity if equity is not None else balance
        self.day_realised = 0.0
        self.day_requests = 0
        self.consecutive_losses = 0
        return True

    @property
    def is_paused_today(self) -> bool:
        # A brand-new state has both fields empty; empty == empty must not make
        # the account look paused before the first FTMO day is anchored.
        return bool(self.day_key) and self.paused_until_day == self.day_key

    def pause_today(self) -> None:
        self.paused_until_day = self.day_key

    def halt(self, reason: str) -> None:
        self.halted_forever = True
        self.halted_reason = reason

    def count_trading_day(self) -> None:
        """Record that this FTMO day now has a position on it."""
        if self.day_key and self.day_key not in self.trading_days:
            self.trading_days.append(self.day_key)

    def remember_plan(self, plan_id: str, keep: int = 400) -> None:
        self.seen_plan_ids.append(plan_id)
        del self.seen_plan_ids[:-keep]

    def open_trades(self) -> list[ManagedTrade]:
        return [trade for trade in self.trades.values() if not trade.closed]
