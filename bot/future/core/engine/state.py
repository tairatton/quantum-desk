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

# TopStep's trading day is the CME day: it starts at 17:00 America/Chicago and
# the daily loss limit resets there. This copy of the module used to carry
# FTMO's Europe/Prague midnight, inherited wholesale from the forex tree, which
# would have anchored `day_start_balance` seven hours into the session and reset
# the daily counter in the middle of the US afternoon.
EXCHANGE_TIMEZONE = ZoneInfo("America/Chicago")
TRADING_DAY_START_HOUR = 17


def trading_day(moment: datetime) -> date:
    """The TopStep trading day containing `moment`.

    A naive timestamp is taken to be UTC rather than local: the machine running
    this is not in Chicago, and guessing its clock is how a session boundary
    ends up half a day out.
    """
    aware = moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment
    local = aware.astimezone(EXCHANGE_TIMEZONE)
    # 17:00 onward already belongs to the next calendar day's session.
    if local.hour >= TRADING_DAY_START_HOUR:
        return (local + timedelta(days=1)).date()
    return local.date()


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
    # Target identities are persisted separately from list order. Pending fills
    # can become visible to MT5 history out of order, so list position is not a
    # safe long-term definition of a leg. The TP1 fields predate the stepped
    # exit fields below and remain for state-file compatibility.
    tp1_position_ticket: int | None = None
    tp1_pending_ticket: int | None = None
    tp2_market_order_ticket: int | None = None
    tp3_market_order_ticket: int | None = None
    tp2_position_ticket: int | None = None
    tp3_position_ticket: int | None = None
    tp2_pending_ticket: int | None = None
    tp3_pending_ticket: int | None = None
    filled_at: str | None = None
    fill_bar_time: str | None = None
    breakeven_done: bool = False
    # TP2 -> TP1 profit lock for the remaining TP3 leg. Kept separate from
    # breakeven_done because the two transitions can happen at different times
    # and both must remain retryable across a restart.
    tp2_lock_done: bool = False
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
    # A state file belongs to exactly one account. Without this binding,
    # switching from a demo to a Combine could carry the old initial balance,
    # trading days and tickets into the new account. ProjectX identifies an
    # account by a numeric id and a name; `account_login`/`account_server` are
    # kept as the storage for those two so an existing state file still loads.
    account_login: int | None = None       # ProjectX account id
    account_server: str = ""               # ProjectX account name
    initial_balance: float = 0.0
    # Highest closed balance observed on this account. Dynamic risk compares
    # current equity against it, and persistence prevents a restart from
    # resetting a drawdown account to the highest risk tier.
    balance_high_water: float = 0.0
    # Highest END-OF-DAY balance, rolled once per trading day. Distinct from
    # `balance_high_water`, which moves the moment a trade closes: TopStep's max
    # loss trails the end-of-day figure, so a profit made and given back inside
    # the same day must not ratchet the floor up. Unused by the forex instance,
    # whose max loss is static.
    eod_balance_high_water: float = 0.0
    # Last offset measured from a live tick. Kept because it cannot be measured
    # while the market is closed, and the news calendar needs it to line up.
    server_utc_offset: float | None = None
    day_key: str = ""                  # TopStep trading day, 17:00 CT boundary
    # Best and worst completed-day P&L, durable because the consistency rule is
    # scored across the whole evaluation: a restart that forgot the best day
    # would report a $3,000 target that the rule has already raised.
    best_day_profit: float = 0.0
    worst_day_loss: float = 0.0
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
    paused_until_day: str = ""         # daily stop hit: resume next trading day
    # Days on which at least one position was opened. Durable like everything
    # else here so a restart cannot lose evaluation progress.
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
        # ProjectX returns {"id": .., "name": ..}; the older MT5 shape is still
        # accepted so an inherited state file and its tests keep working.
        login = int(account.get("id") or account.get("login") or 0)
        server = str(account.get("name") or account.get("server") or "")
        balance = float(account.get("balance") or 0.0)
        if login <= 0 or not server:
            raise ValueError("ProjectX account identity is incomplete "
                             "(need id and name); refusing LIVE trading")
        if self.account_login is None:
            if self.initial_balance > 0 and balance > 0:
                difference = abs(balance - self.initial_balance) / self.initial_balance
                if difference > 0.25:
                    raise ValueError(
                        f"legacy state is anchored at {self.initial_balance:,.2f}, "
                        f"but the account balance is {balance:,.2f}; archive state.json before "
                        "using a different account")
            self.account_login = login
            self.account_server = server
            return True
        if self.account_login != login or self.account_server != server:
            raise ValueError(
                f"state belongs to account {self.account_login} "
                f"({self.account_server}), but this is {login} ({server}); "
                f"use a separate state.json")
        return False

    def anchor_initial_balance(self, balance: float) -> bool:
        """Record the starting balance once, before any limit is evaluated.

        Every TopStep limit is measured from this number, so it cannot be
        inferred from whatever the balance happens to be at the moment a guard
        runs. Without it `max_loss_floor` fell back to current balance: a fresh
        $50,000 account got a $48,000 floor, and the same fresh state at $49,000
        got a $47,000 floor -- a trailing floor walking DOWNWARD, which is not a
        rule any firm has.
        """
        candidate = float(balance or 0.0)
        if self.initial_balance > 0 or candidate <= 0:
            return False
        self.initial_balance = candidate
        self.balance_high_water = max(self.balance_high_water, candidate)
        self.eod_balance_high_water = max(self.eod_balance_high_water, candidate)
        return True

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
        """Start a new trading day at the 17:00 America/Chicago boundary.

        ``evaluation_day`` must come from `trading_day()`. The day that is
        ENDING is closed out first: its final balance sets the end-of-day high
        water mark that TopStep's max loss trails, and its P&L updates the
        durable best/worst day the consistency rule is scored on. Neither of
        those was ever written before, so the trailing floor sat frozen at the
        starting balance for the life of the account.

        The loss streak resets here too. It is a "stand down for the rest of the
        day" rule that can only be cleared by a winning trade — so without this
        reset a day ending on the third loss would pause every day after it.
        """
        key = evaluation_day.isoformat()
        if key == self.day_key:
            return False
        if self.day_key:                       # a real day just finished
            self.close_out_day(balance)
        elif self.initial_balance <= 0:
            self.anchor_initial_balance(balance)
        self.day_key = key
        self.day_start_balance = balance
        self.day_start_equity = equity if equity is not None else balance
        self.day_realised = 0.0
        self.day_requests = 0
        self.consecutive_losses = 0
        self.eod_balance_high_water = max(
            float(self.eod_balance_high_water or 0.0),
            float(self.initial_balance or 0.0))
        return True

    def close_out_day(self, closing_balance: float) -> None:
        """Settle the day that just ended: high-water mark and best/worst day.

        The high water is `max(old, closing balance, initial balance)` and never
        anything else. Taking it from an intraday figure would let a profit made
        and given back inside one day ratchet the floor up, which is exactly the
        difference between TopStep's end-of-day trail and an intraday one.
        """
        closing = float(closing_balance or 0.0)
        self.eod_balance_high_water = max(
            float(self.eod_balance_high_water or 0.0),
            closing,
            float(self.initial_balance or 0.0))
        if self.day_start_balance:
            result = closing - float(self.day_start_balance)
            self.best_day_profit = max(float(self.best_day_profit or 0.0), result)
            self.worst_day_loss = min(float(self.worst_day_loss or 0.0), result)

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
