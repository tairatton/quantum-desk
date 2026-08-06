"""Every tunable the bot has, in one place.

Code defaults stay on the defensive static 0.40% plan. The checked-in local
production profile enables the tested drawdown ladder for gold M15 + M30 and a
capital-tier exit at 30,000. Override any field with an environment variable
named `BOT_<FIELD>` or with `bot/settings.local.json`.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent
STATE_PATH = BOT_DIR / "state.json"
JOURNAL_PATH = BOT_DIR / "journal.jsonl"
LOCAL_SETTINGS = BOT_DIR / "settings.local.json"
# Presence of this file blocks every new entry. Deleting it resumes trading.
KILL_SWITCH = BOT_DIR / "STOP"


@dataclass(frozen=True)
class Settings:
    # --- what to trade -----------------------------------------------------
    symbol: str = "XAUUSD"
    timeframes: tuple[str, ...] = ("M15", "M30")
    # Bars handed to the strategy. Fixed on purpose: the Pine state machine is
    # path dependent, so a moving window would make the plan irreproducible.
    history_bars: int = 3000

    # --- risk --------------------------------------------------------------
    risk_percent: float = 0.40           # per trade, of the initial balance
    max_open_risk_percent: float = 0.80  # sum of live risk across all positions
    max_concurrent_trades: int = 2
    # Optional drawdown ladder. ``risk_percent`` remains the defensive floor;
    # close to the realised balance high-water mark the bot may start faster,
    # then automatically steps down as current equity draws down.
    dynamic_risk_enabled: bool = False
    dynamic_risk_max_percent: float = 1.00
    dynamic_risk_dd1_percent: float = 0.50
    dynamic_risk_tier2_percent: float = 0.75
    dynamic_risk_dd2_percent: float = 1.00
    dynamic_risk_tier3_percent: float = 0.50
    dynamic_risk_dd3_percent: float = 1.50
    # When a higher tier does not fit the remaining exposure/day room, try the
    # next configured tier instead of wasting safe capacity or sizing arbitrarily.
    dynamic_risk_fit_remaining: bool = False

    # --- FTMO objectives, 2-Step, as published on 2026-07-26 ---------------
    # 2-Step: max loss is STATIC from initial capital. The 1-Step product uses an
    # end-of-day trailing max loss and a 3% daily loss instead, so these numbers
    # are wrong for it — see bot/README.md before switching product.
    max_loss_percent: float = 10.0       # hard fail, static, from initial balance
    daily_loss_percent: float = 5.0      # hard fail, equity vs day-start balance
    profit_target_percent: float = 10.0  # 10% step 1, 5% step 2 (verification)
    min_trading_days: int = 4            # days with at least one position opened

    # --- self-imposed stops, tighter than FTMO's ---------------------------
    internal_daily_stop_percent: float = 1.50
    max_consecutive_losses: int = 3
    # Stop opening trades once the profit target and the four trading days are
    # both satisfied. There is no reward for trading on past a pass, only the
    # chance of giving it back or breaking an objective.
    stop_at_target: bool = True

    # --- forbidden-practice compliance -------------------------------------
    # FTMO forbids holding opposing positions on the same or highly correlated
    # instrument. M15 and M30 can disagree, so the later signal is dropped.
    allow_opposing_positions: bool = False
    # "Gap trading" — opening a position within two hours of a closure that
    # lasts two hours or more — is forbidden, which rules out the Friday close.
    weekly_close_weekday: int = 4         # 0 = Monday, 4 = Friday (server clock)
    weekly_close_hour: int = 23           # server hour the week's session ends
    weekly_open_weekday: int = 0          # 0 = Monday, when the week resumes
    weekly_open_hour: int = 1             # server hour gold quotes again
    # Three hours, not the two FTMO requires. The extra hour is deliberate: gold
    # thins out well before the bell, and an entry taken at 21:00 Friday cannot
    # reach TP1 before the gap decides it. Existing positions are never closed to
    # avoid a closure — holding over is what the backtest does.
    blackout_hours_before_close: float = 3.0
    # A closure shorter than this is not "gap trading" under the rule, so it does
    # not trigger the blackout.
    min_closure_hours_for_gap_rule: float = 2.0
    # Holidays and early closes, on the server clock. MT5 5.0.5735 exposes no
    # session schedule, so these are maintained by hand from the broker's notice:
    #   "2026-12-25"                                     shut all day
    #   ["2026-12-24 20:00", "2026-12-26 01:00", "Xmas"]  an exact span
    market_closures: tuple = ()
    # Funded Standard accounts cannot open, close or place a pending order within
    # 2 minutes either side of selected releases, and "gap trading" around major
    # news is forbidden in every phase.
    #
    # The window is deliberately wider than the 2 minutes the rule asks for:
    # 5 minutes before and 3 after. Before matters more than after — the spread on
    # gold widens and liquidity thins well ahead of a release, so an entry taken at
    # T-3 is filled at a price the backtest never saw, whereas by T+3 the book has
    # usually come back. This is a trading decision, not a rule, and it does make
    # live diverge from the backtest, which traded straight through news: expect
    # slightly fewer trades than the study's 2.8/day.
    news_enabled: bool = True
    news_source_url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    news_currencies: tuple[str, ...] = ("USD",)   # gold is a USD instrument
    news_min_impact: str = "high"                 # low | medium | high
    news_minutes_before: int = 5
    news_minutes_after: int = 3
    news_cache_hours: float = 6.0
    # When the calendar cannot be loaded: False keeps trading (correct during the
    # evaluation, where news trading is allowed), True refuses new entries. Set
    # it True once the account is funded and Standard.
    news_require_calendar: bool = False
    # Used only until the bot measures the real offset from a live tick, which it
    # cannot do while the market is closed. Exness servers run EET/EEST (UTC+2/+3);
    # FTMO runs CE(S)T (UTC+1/+2).
    fallback_server_utc_offset: float = 3.0
    news_times: tuple[str, ...] = ()      # extra ISO "2026-08-01 14:30", server time
    # FTMO reviews "risk per trade idea". Two timeframes agreeing is arguably one
    # idea taken twice, so same-direction exposure is capped separately. At 0.80
    # both M15 and M30 may hold a position; setting it to 0.40 enforces one gold
    # position at a time, which is stricter but roughly doubles time-to-target.
    max_risk_per_idea_percent: float = 0.80

    # --- execution ---------------------------------------------------------
    magic: int = 20260726                # the bot only ever touches its own orders
    deviation_points: int = 30           # max slippage accepted on a market order
    entry_grace_seconds: int = 20        # wait after a bar closes before acting
    # Minimum gap between two writes to the broker. `be_33_33_34` sends three
    # orders for one signal, and three in the same instant reads as automated
    # order flooding — brokers answer that with a temporary block, which would
    # strand a partly-placed trade. One second is enough to break the burst and
    # halves the price drift the last leg has to wear: on gold that drift is
    # what the tight `deviation_points` guard rejects.
    # This changes only the timing of the requests: entry, stop, targets and
    # sizing are untouched, so the trade is still the one the study measured.
    write_spacing_seconds: float = 1.0
    # The terminal is allowed roughly 2,000 server requests a day, so the loop
    # sleeps until the next bar close instead of polling continuously.
    poll_seconds: int = 60               # fallback heartbeat only
    # A split trade needs client-side management after TP1; broker-side TP/SL
    # cannot express "move the other stops when this target fills". Checking
    # every three minutes cuts the old 15/30-minute delay without exhausting the
    # terminal's approximate 2,000-request daily allowance.
    split_management_poll_seconds: int = 180
    reconnect_initial_seconds: int = 10
    reconnect_max_seconds: int = 60
    max_requests_per_day: int = 2000
    # Skip an immediate entry when the live price has already run this far past
    # the backtest's fill price, in fractions of the plan's own risk.
    max_entry_slippage_r: float = 0.15

    # --- safety ------------------------------------------------------------
    # There is deliberately no `dry_run` field. It existed, did nothing — `run.py`
    # derives dry-run from the absence of `--live` — and a setting that looks like
    # a safety switch but is ignored is worse than none: `{"dry_run": true}` in
    # settings.local.json would read as safe while `--live` sent real orders.
    # Unknown keys now raise at load, so that mistake fails loudly instead.
    single_leg_fallback_target: int = 2   # TP index when size cannot split 3 ways

    # How the leftover fraction of a lot step is handled. `down` never risks more
    # than asked but discards it: 0.40% of 10,000 wants 0.0191 lots of gold, gets
    # 0.01, and trades at 0.21% — half the intended size, so twice the time to
    # target. `nearest` claims it when doing so is nearly free, guarded by
    # `max_risk_overshoot`; on this account that lifts M15 to 0.42%, over target
    # by 5%. The guard matters: without it the same rule would trade 0.78% on a
    # 2,700 balance, because there half a lot step is most of the position.
    lot_rounding: str = "nearest"
    max_risk_overshoot: float = 0.15     # refuse to round up past +15% of target

    # Which exit the bot trades. This used to be decided by accident: `_split`
    # returned three legs when the lot size allowed and one when it did not, so
    # the exit policy was a side effect of the account balance. On a 10,000
    # account gold cannot split, which happens to give `fixed_tp3` — and that is
    # the exit validation actually selected for M15 and M30. But the account
    # growing past roughly 16,000 would have silently switched it to
    # BE + 33/33/34, a different system from the one measured, with nobody
    # touching a setting. Naming the exit makes that a decision instead.
    #
    #   fixed_tp3       one leg, closed at TP3 (2R). What the study selected.
    #   be_33_33_34     three legs 33/33/34; after TP1, each survivor moves to
    #                   its actual broker fill plus estimated costs/slippage
    #                   and any cumulative negative swap. The stop is refreshed
    #                   while the position remains open.
    #                   Refuses the trade when the balance cannot split.
    #   capital_tier    fixed_tp3 below `split_exit_min_balance`, otherwise
    #                   be_33_33_34. The caller must pass the initial balance,
    #                   never current equity, so the policy cannot flip mid-run.
    #   auto            the old behaviour: split when possible. Not recommended.
    exit_mode: str = "capital_tier"
    split_exit_min_balance: float = 30_000.0

    # --- bookkeeping -------------------------------------------------------
    initial_balance: float = 0.0         # 0 = capture the balance on first run
    tags: tuple[str, ...] = field(default_factory=lambda: ("quantum",))

    def __post_init__(self) -> None:
        errors = []
        finite_risk_fields = {
            "risk_percent": self.risk_percent,
            "dynamic_risk_max_percent": self.dynamic_risk_max_percent,
            "dynamic_risk_tier2_percent": self.dynamic_risk_tier2_percent,
            "dynamic_risk_tier3_percent": self.dynamic_risk_tier3_percent,
            "dynamic_risk_dd1_percent": self.dynamic_risk_dd1_percent,
            "dynamic_risk_dd2_percent": self.dynamic_risk_dd2_percent,
            "dynamic_risk_dd3_percent": self.dynamic_risk_dd3_percent,
            "max_open_risk_percent": self.max_open_risk_percent,
            "max_risk_per_idea_percent": self.max_risk_per_idea_percent,
            "internal_daily_stop_percent": self.internal_daily_stop_percent,
            "daily_loss_percent": self.daily_loss_percent,
            "max_loss_percent": self.max_loss_percent,
            "profit_target_percent": self.profit_target_percent,
        }
        non_finite = [name for name, value in finite_risk_fields.items()
                      if not math.isfinite(float(value))]
        if non_finite:
            errors.append("risk settings must be finite: " + ", ".join(non_finite))
        if not self.symbol.strip():
            errors.append("symbol must not be empty")
        if not self.timeframes:
            errors.append("timeframes must not be empty")
        if self.risk_percent <= 0:
            errors.append("risk_percent must be > 0")
        peak_risk = (self.dynamic_risk_max_percent if self.dynamic_risk_enabled
                     else self.risk_percent)
        if self.max_open_risk_percent < peak_risk:
            errors.append("max_open_risk_percent must be >= the maximum per-setup risk")
        if self.max_risk_per_idea_percent < peak_risk:
            errors.append("max_risk_per_idea_percent must be >= the maximum per-setup risk")
        if self.dynamic_risk_enabled:
            tiers = (self.dynamic_risk_max_percent,
                     self.dynamic_risk_tier2_percent,
                     self.dynamic_risk_tier3_percent,
                     self.risk_percent)
            if not all(value > 0 for value in tiers):
                errors.append("dynamic risk tiers must be positive")
            if not all(a >= b for a, b in zip(tiers, tiers[1:])):
                errors.append("dynamic risk tiers must decrease toward risk_percent")
            drawdowns = (self.dynamic_risk_dd1_percent,
                         self.dynamic_risk_dd2_percent,
                         self.dynamic_risk_dd3_percent)
            if not (0 < drawdowns[0] < drawdowns[1] < drawdowns[2]):
                errors.append("dynamic risk drawdown thresholds must increase")
        if not 0 < self.internal_daily_stop_percent <= self.daily_loss_percent:
            errors.append("internal_daily_stop_percent must be > 0 and <= daily_loss_percent")
        if self.daily_loss_percent <= 0 or self.max_loss_percent <= 0:
            errors.append("daily_loss_percent and max_loss_percent must be > 0")
        if self.max_concurrent_trades < 1:
            errors.append("max_concurrent_trades must be >= 1")
        if self.max_consecutive_losses < 1:
            errors.append("max_consecutive_losses must be >= 1")
        if self.profit_target_percent <= 0 or self.min_trading_days < 1:
            errors.append("profit target and minimum trading days must be positive")
        if self.exit_mode not in {"fixed_tp3", "be_33_33_34", "capital_tier", "auto"}:
            errors.append(f"unsupported exit_mode: {self.exit_mode}")
        if self.split_exit_min_balance <= 0:
            errors.append("split_exit_min_balance must be > 0")
        if self.lot_rounding not in {"down", "nearest"}:
            errors.append(f"unsupported lot_rounding: {self.lot_rounding}")
        if self.reconnect_initial_seconds <= 0:
            errors.append("reconnect_initial_seconds must be > 0")
        if self.reconnect_max_seconds < self.reconnect_initial_seconds:
            errors.append("reconnect_max_seconds must be >= reconnect_initial_seconds")
        if self.write_spacing_seconds < 0:
            errors.append("write_spacing_seconds must be >= 0")
        if self.split_management_poll_seconds <= 0:
            errors.append("split_management_poll_seconds must be > 0")
        if self.max_requests_per_day < 1:
            errors.append("max_requests_per_day must be >= 1")
        if self.news_minutes_before < 0 or self.news_minutes_after < 0:
            errors.append("news windows must not be negative")
        if errors:
            raise ValueError("invalid bot settings: " + "; ".join(errors))

    def resolved_exit_mode(self, initial_balance: float | None = None) -> str:
        """Concrete exit policy selected from the account's anchored capital."""
        if self.exit_mode != "capital_tier":
            return self.exit_mode
        capital = self.initial_balance if initial_balance is None else initial_balance
        return ("be_33_33_34" if capital >= self.split_exit_min_balance
                else "fixed_tp3")

    def leg_weights_for(self, initial_balance: float | None = None) -> tuple[float, ...]:
        """Leg weights for the concrete policy active on this account."""
        if self.resolved_exit_mode(initial_balance) == "fixed_tp3":
            return (1.0,)
        return (0.33, 0.33, 0.34)

    @property
    def leg_weights(self) -> tuple[float, ...]:
        """Compatibility accessor; runtime code should pass anchored capital."""
        return self.leg_weights_for()


def _coerce(raw: str, current):
    if isinstance(current, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current, tuple):
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    return type(current)(raw)


def load() -> Settings:
    """Defaults, then settings.local.json, then BOT_* environment variables.

    A key beginning with `_` is a comment and is ignored. JSON has no comment
    syntax, and every value in that file is a decision with a reason behind it —
    which server clock was measured, why the risk is what it is — so there has to
    be somewhere to write the reason down next to the number. Any *other* unknown
    key still raises: a typo in a safety limit must not pass silently.
    """
    values: dict[str, object] = {}
    if LOCAL_SETTINGS.exists():
        values.update({key: value for key, value
                       in json.loads(LOCAL_SETTINGS.read_text(encoding="utf-8")).items()
                       if not key.startswith("_")})
    defaults = Settings()
    for spec in fields(Settings):
        env = os.getenv(f"BOT_{spec.name.upper()}")
        if env is not None:
            values[spec.name] = _coerce(env, getattr(defaults, spec.name))
    for key in ("timeframes", "tags", "news_currencies"):
        if isinstance(values.get(key), list):
            values[key] = tuple(values[key])
    return Settings(**values)
