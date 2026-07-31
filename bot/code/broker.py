"""MetaTrader 5 trading session for the bot.

`xau.mt5_source.connection()` opens and shuts the terminal down per call, which
suits one-shot analysis but not a loop that must hold orders open. This module
keeps one session for the process lifetime and exposes only the operations the
bot needs. Every write goes through `_send`, so dry-run is enforced in a single
place rather than at each call site.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import time

import pandas as pd

from xau import config, mt5_source
from xau.mt5_source import MT5Error


class OrderRejected(MT5Error):
    """A broker accepted the request but refused to carry out the order."""


def _configured_mt5_login() -> dict[str, object]:
    """Return explicit MT5 login details from the process environment.

    Credentials are deliberately not read from source code or settings.local.json.
    Set all three BOT_MT5_LOGIN, BOT_MT5_PASSWORD and BOT_MT5_SERVER variables to
    make every bot start attach to the intended account; leave all three unset to
    retain MT5's currently logged-in account behaviour.
    """
    raw_login = os.getenv("BOT_MT5_LOGIN", "").strip()
    password = os.getenv("BOT_MT5_PASSWORD", "")
    server = os.getenv("BOT_MT5_SERVER", "").strip()
    provided = (bool(raw_login), bool(password), bool(server))
    if not any(provided):
        return {}
    if not all(provided):
        raise MT5Error(
            "MT5 account configuration is incomplete. Set BOT_MT5_LOGIN, "
            "BOT_MT5_PASSWORD and BOT_MT5_SERVER together."
        )
    try:
        login = int(raw_login)
    except ValueError as exc:
        raise MT5Error("BOT_MT5_LOGIN must be a numeric MT5 account number.") from exc
    if login <= 0:
        raise MT5Error("BOT_MT5_LOGIN must be a positive MT5 account number.")
    return {"login": login, "password": password, "server": server}


@dataclass(frozen=True)
class SymbolSpec:
    name: str                # broker name, e.g. XAUUSDm
    digits: int
    point: float
    volume_min: float
    volume_max: float
    volume_step: float
    value_per_point: float   # account currency per 1.0 price point, per 1.0 lot
    stops_level_points: float
    filling: int


@dataclass(frozen=True)
class Position:
    ticket: int
    symbol: str
    direction: int           # +1 long, -1 short
    volume: float
    price_open: float
    stop: float
    take_profit: float
    profit: float
    swap: float
    comment: str
    opened_at: datetime


class Broker:
    """Thin, explicit wrapper. Raises MT5Error rather than returning None."""

    def __init__(self, symbol_key: str, magic: int, deviation: int, dry_run: bool,
                 write_spacing_seconds: float = 0.0):
        self.symbol_key = symbol_key.upper()
        self.magic = magic
        self.deviation = deviation
        self.dry_run = dry_run
        # Minimum gap between two writes. A `be_33_33_34` entry sends three orders
        # back to back, which brokers read as automated order flooding and answer
        # with a temporary block. Spacing them is a compliance measure, not a
        # trading one: nothing about the plan — entry, stop, targets, sizing —
        # changes, only how fast the legs leave.
        self.write_spacing_seconds = max(0.0, write_spacing_seconds)
        self._last_write: float | None = None
        self._mt = None
        self._spec: SymbolSpec | None = None
        # FTMO caps an EA at roughly 2,000 server requests a day. Sleeping to the
        # next bar close keeps the bot far under it, but "far under" was an
        # estimate nothing measured — this counts them so the limit is a fact.
        self._requests = 0
        # Last distinct tick timestamp and the local clock reading when it first
        # appeared, so a feed that stops advancing can be told from a live one.
        self._tick_stamp: datetime | None = None
        self._tick_first_seen: datetime | None = None
        self._filled_position_times: dict[int, datetime] = {}

    @property
    def requests(self) -> int:
        return self._requests

    def take_requests(self) -> int:
        """Hand the tally to the caller and start counting again."""
        spent, self._requests = self._requests, 0
        return spent

    # -- session ----------------------------------------------------------
    def __enter__(self) -> "Broker":
        self._connect()
        return self

    def _connect(self) -> None:
        """Initialize MT5 and rebuild broker-specific symbol metadata."""
        if not mt5_source.MT5_IMPORTED:
            raise MT5Error("MetaTrader5 package missing.  Fix: pip install MetaTrader5")
        mt5 = mt5_source.mt5
        terminal = mt5_source.find_terminal()
        kwargs = {"timeout": config.MT5_INIT_TIMEOUT_MS}
        if terminal is not None:
            kwargs["path"] = str(terminal)
        kwargs.update(_configured_mt5_login())
        if not mt5.initialize(**kwargs):
            code, desc = mt5.last_error()
            raise MT5Error(f"MT5 initialize() failed: ({code}) {desc}")
        info = mt5.terminal_info()
        if info is not None and not info.connected:
            mt5.shutdown()
            raise MT5Error("MT5 terminal is running but not connected to the broker.")
        if info is not None and not info.trade_allowed and not self.dry_run:
            mt5.shutdown()
            raise MT5Error("Algorithmic trading is disabled in the terminal.\n"
                           "  Tools > Options > Expert Advisors > Allow algorithmic trading")
        self._mt = mt5
        self._spec = self._read_spec()
        self._tick_stamp = None
        self._tick_first_seen = None

    def reconnect(self) -> None:
        """Reinitialize the terminal after a network/terminal interruption.

        Existing SL/TP and pending orders live at the broker. Reconnecting only
        restores observation and client-side management; it never resends an
        order by itself.
        """
        if self._mt is not None:
            self._mt.shutdown()
        self._mt = None
        self._spec = None
        self._connect()

    def __exit__(self, *_exc) -> None:
        if self._mt is not None:
            self._mt.shutdown()
            self._mt = None

    @property
    def mt(self):
        if self._mt is None:
            raise MT5Error("Broker used outside its `with` block.")
        return self._mt

    @property
    def spec(self) -> SymbolSpec:
        if self._spec is None:
            raise MT5Error("Broker used outside its `with` block.")
        return self._spec

    def _read_spec(self) -> SymbolSpec:
        name = mt5_source.resolve_symbol(self._mt, self.symbol_key)
        info = self._mt.symbol_info(name)
        if info is None:
            raise MT5Error(f"symbol_info({name}) returned nothing.")
        if info.trade_tick_size <= 0 or info.trade_tick_value <= 0:
            raise MT5Error(f"{name} reports no tick value; cannot size positions safely.")
        # Prefer a filling mode the symbol advertises; bit 1 = FOK, bit 2 = IOC.
        filling = (self._mt.ORDER_FILLING_FOK if info.filling_mode & 1
                   else self._mt.ORDER_FILLING_IOC if info.filling_mode & 2
                   else self._mt.ORDER_FILLING_RETURN)
        return SymbolSpec(
            name=name, digits=info.digits, point=info.point,
            volume_min=info.volume_min, volume_max=info.volume_max,
            volume_step=info.volume_step,
            value_per_point=info.trade_tick_value / info.trade_tick_size,
            stops_level_points=float(info.trade_stops_level), filling=filling,
        )

    # -- reads ------------------------------------------------------------
    def account(self) -> dict:
        self._requests += 1
        info = self.mt.account_info()
        if info is None:
            raise MT5Error("account_info() returned nothing.")
        margin_mode = int(getattr(info, "margin_mode", -1))
        hedging_mode = int(getattr(
            self.mt, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2
        ))
        return {"login": info.login, "server": info.server, "currency": info.currency,
                "balance": float(info.balance), "equity": float(info.equity),
                "margin_free": float(info.margin_free),
                "margin_mode": margin_mode,
                "is_hedging": margin_mode == hedging_mode}

    def tick(self) -> dict:
        self._requests += 1
        quote = self.mt.symbol_info_tick(self.spec.name)
        if quote is None:
            raise MT5Error(f"No tick for {self.spec.name}; market may be closed.")
        return {"bid": float(quote.bid), "ask": float(quote.ask),
                "spread": float(quote.ask - quote.bid),
                # Broker server time. FTMO's trading day rolls over at this clock's
                # midnight, so every daily calculation must use it, not local time.
                "server_time": datetime.fromtimestamp(quote.time, tz=timezone.utc)
                .replace(tzinfo=None)}

    def server_utc_offset(self, max_staleness_minutes: int = 10) -> float | None:
        """Hours the broker clock runs ahead of UTC, or None if unmeasurable.

        The economic calendar publishes in UTC while every timestamp the terminal
        reports is on the server clock, so one has to be shifted onto the other.
        MT5 exposes no offset, but the gap between the server's own reading and
        real UTC is exactly it — *while quotes are flowing*. Over a weekend the
        last tick is hours or days old and that subtraction returns nonsense
        (a closed market measured -43.5h here), so a stale tick returns None and
        the caller falls back to the last known good value.
        """
        if self.feed_stale_minutes(max_staleness_minutes) is not None:
            return None
        quote = self.tick()
        delta = quote["server_time"] - datetime.now(timezone.utc).replace(tzinfo=None)
        hours = delta.total_seconds() / 3600
        if not -12.5 <= hours <= 14.5:
            return None
        rounded = round(hours * 2) / 2
        # A fresh tick can still sit a few minutes back on an illiquid symbol.
        if abs(hours - rounded) * 60 > max_staleness_minutes:
            return None
        return rounded

    def feed_stale_minutes(self, threshold_minutes: int = 10) -> float | None:
        """Minutes the tick has been frozen, or None while quotes are flowing.

        The rounding test above cannot see staleness on its own. A tick lagging
        by exactly half an hour makes a UTC+3 server measure 2.5, which rounds to
        2.5 with no residue and sails through — so a stalled feed was read as a
        timezone change and written to `state.json`. That happened on 2026-07-27:
        the feed froze at 23:49:57 server for roughly an hour and a half and the
        offset walked +3 -> +2.5 -> +2 -> +3, silently aiming every news blackout
        window up to an hour away from the release it was meant to cover.

        Staleness is therefore measured the only way it can be: by whether the
        tick's own timestamp advances while the local clock does.
        """
        stamp = self.tick()["server_time"]
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if stamp != self._tick_stamp:
            self._tick_stamp, self._tick_first_seen = stamp, now
            return None
        if self._tick_first_seen is None:
            self._tick_first_seen = now
            return None
        frozen = (now - self._tick_first_seen).total_seconds() / 60
        return frozen if frozen > threshold_minutes else None

    def bars(self, timeframe: str, count: int) -> pd.DataFrame:
        self._requests += 1
        rates = self.mt.copy_rates_from_pos(
            self.spec.name, config.TIMEFRAMES[timeframe.upper()], 0, count)
        if rates is None or len(rates) == 0:
            code, desc = self.mt.last_error()
            raise MT5Error(f"No {timeframe} bars for {self.spec.name}: ({code}) {desc}")
        frame = pd.DataFrame(rates)
        frame["time"] = pd.to_datetime(frame["time"], unit="s")
        # The final bar is still forming; the strategy only ever sees closed bars.
        return frame.iloc[:-1].reset_index(drop=True)

    def margin_for(self, direction: int, volume: float) -> float | None:
        """Margin one leg would tie up, or None if the terminal cannot say.

        Worth asking before sending rather than after: gold on a Swing account is
        leveraged 1:9, and a rejection part-way through placing three legs is the
        failure that leaves a trade half open.
        """
        self._requests += 1
        quote = self.tick()
        margin = self.mt.order_calc_margin(
            self.mt.ORDER_TYPE_BUY if direction == 1 else self.mt.ORDER_TYPE_SELL,
            self.spec.name, self._volume(volume),
            quote["ask"] if direction == 1 else quote["bid"])
        return None if margin is None else float(margin)

    def positions(self) -> list[Position]:
        self._requests += 1
        raw = self.mt.positions_get(symbol=self.spec.name)
        if raw is None:
            code, desc = self.mt.last_error()
            raise MT5Error(
                f"positions_get({self.spec.name}) failed: ({code}) {desc}"
            )
        return [Position(
            ticket=p.ticket, symbol=p.symbol,
            direction=1 if p.type == self.mt.POSITION_TYPE_BUY else -1,
            volume=float(p.volume), price_open=float(p.price_open),
            stop=float(p.sl), take_profit=float(p.tp), profit=float(p.profit),
            swap=float(p.swap),
            comment=p.comment,
            opened_at=datetime.fromtimestamp(p.time, tz=timezone.utc).replace(tzinfo=None),
        ) for p in raw if p.magic == self.magic]

    def pending_orders(self) -> list[dict]:
        self._requests += 1
        raw = self.mt.orders_get(symbol=self.spec.name)
        if raw is None:
            code, desc = self.mt.last_error()
            raise MT5Error(
                f"orders_get({self.spec.name}) failed: ({code}) {desc}"
            )
        type_names = {
            self.mt.ORDER_TYPE_BUY_LIMIT: "BUY_LIMIT",
            self.mt.ORDER_TYPE_SELL_LIMIT: "SELL_LIMIT",
            self.mt.ORDER_TYPE_BUY_STOP: "BUY_STOP",
            self.mt.ORDER_TYPE_SELL_STOP: "SELL_STOP",
        }
        orders = []
        for order in raw:
            if order.magic != self.magic:
                continue
            expiration = int(getattr(order, "time_expiration", 0) or 0)
            orders.append({
                "ticket": int(order.ticket),
                "type": int(order.type),
                "type_name": type_names.get(order.type, f"TYPE_{order.type}"),
                "price": float(order.price_open),
                "stop": float(getattr(order, "sl", 0.0) or 0.0),
                "take_profit": float(getattr(order, "tp", 0.0) or 0.0),
                "volume": float(order.volume_current),
                "comment": order.comment,
                "expires_at": (datetime.fromtimestamp(expiration, tz=timezone.utc)
                               .replace(tzinfo=None) if expiration else None),
            })
        return orders

    def filled_order_positions(self, order_tickets: list[int],
                               since: datetime | None = None) -> dict[int, int]:
        """Map order/deal recovery tickets to their MT5 position identifiers.

        Market execution can return a deal ticket without a usable order ticket,
        while pending execution starts from an order ticket. Neither identifier
        is guaranteed to equal the resulting position ticket, so deal history is
        the authoritative bridge for both forms.
        """
        wanted = {int(ticket) for ticket in order_tickets}
        if not wanted:
            return {}
        until = datetime.now() + timedelta(days=1)
        start = since or (until - timedelta(days=30))
        self._requests += 1
        deals = self.mt.history_deals_get(start, until)
        if deals is None:
            code, desc = self.mt.last_error()
            raise MT5Error(
                f"history_deals_get(order mapping) failed: ({code}) {desc}"
            )
        mapped = {}
        for deal in deals:
            if deal.magic != self.magic or deal.symbol != self.spec.name:
                continue
            position_ticket = int(getattr(deal, "position_id", 0) or 0)
            if not position_ticket:
                continue
            order_ticket = int(getattr(deal, "order", 0) or 0)
            deal_ticket = int(getattr(deal, "ticket", 0) or 0)
            raw_time = int(getattr(deal, "time", 0) or 0)
            deal_time = (datetime.fromtimestamp(raw_time, tz=timezone.utc)
                         .replace(tzinfo=None) if raw_time else None)
            if order_ticket in wanted:
                mapped[order_ticket] = position_ticket
                if deal_time is not None:
                    self._filled_position_times[position_ticket] = deal_time
            if deal_ticket in wanted:
                mapped[deal_ticket] = position_ticket
                if deal_time is not None:
                    self._filled_position_times[position_ticket] = deal_time
        return mapped

    def filled_position_time(self, position_ticket: int) -> datetime | None:
        """Deal-history fill time cached by filled_order_positions()."""
        return self._filled_position_times.get(int(position_ticket))

    def account_cashflow_since(self, since_server: datetime) -> float:
        """Net balance-changing cash flow since a broker-server timestamp.

        This intentionally includes every symbol and magic number. FTMO's
        daily-loss reference belongs to the whole account, so commissions,
        swaps, deposits and any non-bot trade must be reflected when a restart
        reconstructs the balance recorded at 00:00 CE(S)T.
        """
        until = datetime.now() + timedelta(days=1)
        start = since_server - timedelta(days=1)
        self._requests += 1
        deals = self.mt.history_deals_get(start, until)
        if deals is None:
            code, desc = self.mt.last_error()
            raise MT5Error(
                f"history_deals_get(account cash flow) failed: ({code}) {desc}"
            )
        total = 0.0
        for deal in deals:
            deal_time = datetime.fromtimestamp(
                deal.time, tz=timezone.utc,
            ).replace(tzinfo=None)
            if deal_time < since_server:
                continue
            total += (
                float(getattr(deal, "profit", 0.0) or 0.0)
                + float(getattr(deal, "commission", 0.0) or 0.0)
                + float(getattr(deal, "swap", 0.0) or 0.0)
                + float(getattr(deal, "fee", 0.0) or 0.0)
            )
        return total

    def finished_order_states(self, order_tickets: list[int],
                              since: datetime | None = None) -> dict[int, str]:
        """Return terminal states for pending orders no longer in the live book."""
        wanted = {int(ticket) for ticket in order_tickets}
        if not wanted:
            return {}
        until = datetime.now() + timedelta(days=1)
        start = since or (until - timedelta(days=30))
        self._requests += 1
        orders = self.mt.history_orders_get(start, until)
        if orders is None:
            code, desc = self.mt.last_error()
            raise MT5Error(
                f"history_orders_get(states) failed: ({code}) {desc}"
            )
        state_names = {
            self.mt.ORDER_STATE_FILLED: "FILLED",
            self.mt.ORDER_STATE_CANCELED: "CANCELED",
            self.mt.ORDER_STATE_REJECTED: "REJECTED",
            self.mt.ORDER_STATE_EXPIRED: "EXPIRED",
        }
        return {
            int(order.ticket): state_names.get(order.state, f"STATE_{order.state}")
            for order in orders
            if order.magic == self.magic and order.symbol == self.spec.name
            and int(order.ticket) in wanted
        }
    def closed_deals(self, since: datetime) -> list[dict]:
        """Deals in a window, with cost and entry/exit classification.

        `deal.profit` is the price result only: MT5 reports commission and swap
        as separate fields. Scoring on profit alone would flatter every live R
        against a backtest that has to be beaten by real money, so `net` carries
        all three and is what the R calculation uses.

        Some terminals encode broker wall-clock time into deal epochs, which can
        place a fresh deal several hours ahead of the machine clock. A one-day
        future cushion covers every timezone offset; magic/symbol/ticket filters
        still restrict the returned records to this bot.
        """
        until = datetime.now() + timedelta(days=1)
        self._requests += 1
        deals = self.mt.history_deals_get(since, until)
        if deals is None:
            code, desc = self.mt.last_error()
            raise MT5Error(
                f"history_deals_get(closed deals) failed: ({code}) {desc}"
            )
        exit_entries = {
            int(getattr(self.mt, "DEAL_ENTRY_OUT", 1)),
            int(getattr(self.mt, "DEAL_ENTRY_INOUT", 2)),
            int(getattr(self.mt, "DEAL_ENTRY_OUT_BY", 3)),
        }
        return [{"ticket": d.ticket, "order": d.order, "position": d.position_id,
                 "volume": float(d.volume),
                 "price": float(d.price), "profit": float(d.profit),
                 "commission": float(d.commission), "swap": float(d.swap),
                 "fee": float(getattr(d, "fee", 0.0) or 0.0),
                 "net": (float(d.profit) + float(d.commission)
                         + float(d.swap)
                         + float(getattr(d, "fee", 0.0) or 0.0)),
                 "comment": d.comment, "entry": d.entry,
                 "is_exit": int(d.entry) in exit_entries,
                 "time": datetime.fromtimestamp(d.time, tz=timezone.utc).replace(tzinfo=None)}
                for d in deals if d.magic == self.magic and d.symbol == self.spec.name]

    # -- writes -----------------------------------------------------------
    def _pace(self) -> None:
        """Hold off until `write_spacing_seconds` have passed since the last write.

        Called at the top of every write method rather than inside `_send`,
        because `market_entry` and `close` read a quote before building the
        request: sleeping after that would price the order off a tick that is
        already `write_spacing_seconds` old, and gold moves in two seconds.

        Dry runs never wait — nothing reaches the broker, so there is nothing to
        pace, and tests would pay the delay for no reason.
        """
        if self.dry_run or self.write_spacing_seconds <= 0 or self._last_write is None:
            return
        waited = time.monotonic() - self._last_write
        remaining = self.write_spacing_seconds - waited
        if remaining > 0:
            print(f"[ORDER_SPACING] waiting {remaining:.2f}s before the next write")
            time.sleep(remaining)

    def _send(self, request: dict, what: str) -> dict:
        if self.dry_run:
            print(f"[ORDER_SIMULATED] action={what!r} request={request}")
            return {"dry_run": True, "request": request}
        self._requests += 1
        # Stamped before the call, not after, so a slow or failed `order_send`
        # cannot stretch the gap: what the broker rates is when requests arrive.
        # A rejection still counts — it was a request either way.
        self._last_write = time.monotonic()
        result = self.mt.order_send(request)
        if result is None:
            code, desc = self.mt.last_error()
            raise MT5Error(f"{what} got no result: ({code}) {desc}")
        ok = result.retcode in (self.mt.TRADE_RETCODE_DONE, self.mt.TRADE_RETCODE_PLACED)
        if not ok:
            raise OrderRejected(f"{what} rejected: retcode={result.retcode} {result.comment}")
        print(f"[ORDER_SUBMITTED] action={what!r} ticket={result.order or result.deal} "
              f"price={float(result.price or 0):.{self.spec.digits}f}")
        return {"dry_run": False, "retcode": result.retcode,
                "order": result.order, "deal": result.deal, "price": result.price,
                "volume": float(getattr(result, "volume", 0.0) or 0.0)}

    def _volume(self, volume: float) -> float:
        """Round to the symbol's own step, not a hard-coded two decimals.

        A step of 0.001 turned 0.003 lots into 0.0 under `round(volume, 2)`, which
        the broker rejects — and a rejection here is what strands a partly-placed
        trade.
        """
        step = self.spec.volume_step or 0.01
        decimals = max(0, len(f"{step:.8f}".rstrip("0").split(".")[1]))
        return round(volume, decimals)

    def market_entry(self, direction: int, volume: float, stop: float,
                     take_profit: float, comment: str,
                     worst_price: float | None = None) -> dict:
        self._pace()
        quote = self.tick()
        price = quote["ask"] if direction == 1 else quote["bid"]
        if worst_price is not None and (
                (direction == 1 and price > worst_price)
                or (direction == -1 and price < worst_price)):
            raise OrderRejected(
                f"market price {price:.{self.spec.digits}f} passed the per-leg "
                f"limit {worst_price:.{self.spec.digits}f}"
            )
        return self._send({
            "action": self.mt.TRADE_ACTION_DEAL, "symbol": self.spec.name,
            "volume": self._volume(volume),
            "type": self.mt.ORDER_TYPE_BUY if direction == 1 else self.mt.ORDER_TYPE_SELL,
            "price": price,
            "sl": round(stop, self.spec.digits), "tp": round(take_profit, self.spec.digits),
            "deviation": self.deviation, "magic": self.magic, "comment": comment[:31],
            "type_time": self.mt.ORDER_TIME_GTC, "type_filling": self.spec.filling,
        }, f"market {'buy' if direction == 1 else 'sell'} {volume}")

    @staticmethod
    def _expiration_stamp(moment: datetime) -> int:
        """Server-clock datetime -> the integer `order_send` will accept.

        The MetaTrader5 binding rejects a `datetime` in the `expiration` field
        with `(-2) Invalid "expiration" argument` — it wants seconds. Every limit
        order the bot placed on FTMO failed on exactly this, so no retracement
        entry ever reached the broker while it read as working locally.

        Seconds in whose clock matters too: `tick.time` is the server's wall
        clock counted as though it were UTC, and `tick()` decodes it that way.
        The inverse has to match, or a UTC+3 server would get an expiry three
        hours out of place.
        """
        return int(moment.replace(tzinfo=timezone.utc).timestamp())

    def limit_entry(self, direction: int, volume: float, price: float, stop: float,
                    take_profit: float, expires_at: datetime, comment: str) -> dict:
        self._pace()
        return self._send({
            "action": self.mt.TRADE_ACTION_PENDING, "symbol": self.spec.name,
            "volume": self._volume(volume),
            "type": (self.mt.ORDER_TYPE_BUY_LIMIT if direction == 1
                     else self.mt.ORDER_TYPE_SELL_LIMIT),
            "price": round(price, self.spec.digits),
            "sl": round(stop, self.spec.digits), "tp": round(take_profit, self.spec.digits),
            "magic": self.magic, "comment": comment[:31],
            "type_time": self.mt.ORDER_TIME_SPECIFIED,
            "expiration": self._expiration_stamp(expires_at),
            "type_filling": self.spec.filling,
        }, f"limit {'buy' if direction == 1 else 'sell'} {volume} @ {price}")

    def move_stop(self, position: Position, stop: float) -> dict:
        """Only ever called to tighten. Widening a stop is not implemented."""
        # MT5 encodes a missing SL as zero. Any real stop is an improvement from
        # no protection, including a SELL stop whose numeric price is > 0.
        improves = (
            not position.stop
            or (stop > position.stop if position.direction == 1
                else stop < position.stop)
        )
        if not improves:
            raise OrderRejected(
                f"refusing to widen stop on #{position.ticket}: "
                f"{position.stop} -> {stop}"
            )
        self._pace()
        return self._send({
            "action": self.mt.TRADE_ACTION_SLTP, "symbol": self.spec.name,
            "position": position.ticket, "sl": round(stop, self.spec.digits),
            "tp": round(position.take_profit, self.spec.digits),
        }, f"move stop #{position.ticket} -> {stop}")

    def close(self, position: Position, reason: str) -> dict:
        self._pace()
        quote = self.tick()
        return self._send({
            "action": self.mt.TRADE_ACTION_DEAL, "symbol": self.spec.name,
            "position": position.ticket, "volume": position.volume,
            "type": (self.mt.ORDER_TYPE_SELL if position.direction == 1
                     else self.mt.ORDER_TYPE_BUY),
            "price": quote["bid"] if position.direction == 1 else quote["ask"],
            "deviation": self.deviation, "magic": self.magic, "comment": reason[:31],
            "type_time": self.mt.ORDER_TIME_GTC, "type_filling": self.spec.filling,
        }, f"close #{position.ticket} ({reason})")

    def cancel(self, ticket: int, reason: str) -> dict:
        self._pace()
        return self._send({"action": self.mt.TRADE_ACTION_REMOVE, "order": ticket},
                          f"cancel order #{ticket} ({reason})")
