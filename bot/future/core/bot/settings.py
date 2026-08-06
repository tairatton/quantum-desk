"""Every tunable the futures instance has, in one place.

Deliberately a separate dataclass from `bot.settings.Settings` rather than
a subclass of it. The two venues agree on almost nothing that matters: TopStep
sizes in whole contracts and denominates its limits in dollars, FTMO sizes in
lots and denominates in percent of the initial balance. Sharing one class would
mean every field carrying "…unless futures", and one careless default would
reach the live forex account.

The environment override prefix is `FUT_`, not `BOT_`. Setting `BOT_RISK_PERCENT`
in a shell must not silently resize futures orders.

Override any field with `FUT_<FIELD>` or with `bot/settings.local.json`.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

FUTURE_DIR = Path(__file__).resolve().parent
STATE_PATH = FUTURE_DIR / "state.json"
JOURNAL_PATH = FUTURE_DIR / "journal.jsonl"
LOCAL_SETTINGS = FUTURE_DIR / "settings.local.json"
LIVE_LOCK_PATH = FUTURE_DIR / ".live.lock"
# Presence of this file blocks every new entry. Deleting it resumes trading.
# Separate from the forex kill switch on purpose: stopping one must not stop
# the other, and neither must be stopped by accident.
KILL_SWITCH = FUTURE_DIR / "STOP"

ENV_PREFIX = "FUT_"


@dataclass(frozen=True)
class Settings:
    # --- what to trade -----------------------------------------------------
    # ProjectX names a contract like "CON.F.US.MGC.Z26"; `contract_symbol` is the
    # root the bot searches for and `contract_id` pins the exact expiry once
    # resolved. Roll is manual on purpose: an automatic roll would change the
    # instrument under a live position.
    contract_symbol: str = "MGC"
    contract_id: str = ""
    timeframes: tuple[str, ...] = ("M15", "M30")
    history_bars: int = 3000

    # --- contract economics ------------------------------------------------
    # MGC (Micro Gold, 10 troy oz): 0.10 per tick, $1.00 per tick, so $10.00 per
    # dollar of gold. These are contract facts, not preferences -- confirm
    # against the exchange spec before trading a different root, because sizing
    # divides by them.
    tick_size: float = 0.10
    tick_value: float = 1.00
    # Whole contracts only. There is no 0.01 of a future.
    contract_step: int = 1
    min_contracts: int = 1
    # TopStep publishes ONE simultaneous-position limit in two units: 5 minis or
    # 50 micros, at the documented 10:1 ratio. Storing a single `max_contracts`
    # forced a choice between them, and the mini number was configured while a
    # micro was traded -- understating the cap tenfold. Both are held here and
    # `guardrails.contract_cap` picks the one that matches the contract.
    max_mini_contracts: int = 5
    max_micro_contracts: int = 50
    # Micro roots are M-prefixed at CME (MGC, MNQ, MES, MCL). Set explicitly
    # rather than inferred when trading something the prefix rule does not fit.
    micro_contract: bool | None = None

    # --- risk ---------------------------------------------------------------
    # Denominated in dollars, not percent: TopStep's limits are dollar amounts
    # that do not scale with equity, so expressing risk as a percentage of a
    # moving balance would drift against the rule that actually ends the account.
    risk_dollars: float = 200.0
    # The firm's daily loss limit is $1,000, and this must stay under it: two
    # concurrent stops are one day's loss. The remaining-room guard binds first
    # in practice (a day's room is the internal stop); this static cap remains
    # a second independent exposure check.
    max_open_risk_dollars: float = 800.0
    max_concurrent_trades: int = 2
    # The same drawdown ladder the forex instance runs, in dollars instead of
    # percent. `risk_dollars` stays the defensive floor; close to the realised
    # balance high-water mark the bot may start larger, then steps down as the
    # account draws down. The tiers below mirror the forex ratios exactly --
    # 1.00 / 0.75 / 0.50 / 0.40% of a 50,000 account -- because the technique is
    # meant to be identical at both venues and only the arithmetic differs.
    # On by default: the flat $200 default meant the account always requested the
    # floor tier and the configured ladder never ran at all.
    dynamic_risk_enabled: bool = True
    # The top tier is bounded by `internal_daily_stop_dollars`, not by the forex
    # ratio it was copied from. A $500 tier under a $400 daily stop is a size
    # the first trade of any day can never reach -- the remaining-room fit would
    # quietly drop it to $375 every time -- so the ladder advertised a number it
    # never used. Sizing the top tier to the daily stop makes the configuration
    # mean what it says.
    dynamic_risk_max_dollars: float = 400.0
    dynamic_risk_tier2_dollars: float = 300.0
    dynamic_risk_tier3_dollars: float = 250.0
    dynamic_risk_dd1_dollars: float = 250.0      # forex 0.50% drawdown
    dynamic_risk_dd2_dollars: float = 500.0      # forex 1.00%
    dynamic_risk_dd3_dollars: float = 750.0      # forex 1.50%
    dynamic_risk_fit_remaining: bool = True
    # Sizes below the floor tier, used ONLY when the room left cannot fit
    # `risk_dollars`. Without them an account that draws down to within one
    # daily stop of its floor can never trade again: the smallest configured
    # size no longer fits, so it stands there until the horizon ends -- alive,
    # not breached, and permanently unable to pass. These let it grind back.
    # Measured: they turn 6.5% of flat-regime paths from stalled into passed,
    # and cost nothing in failures.
    recovery_risk_dollars: tuple[float, ...] = (100.0, 50.0)

    # --- TopStep account rules ---------------------------------------------
    # ESTIMATES for a 50K Combine, and the single most important thing to verify
    # before going live: TopStep publishes these per account size and changes
    # them. Wrong here means the bot thinks it is safe while the account is
    # already gone. Confirm each against the current TopStep rulebook.
    account_size: float = 50_000.0
    daily_loss_limit_dollars: float = 1_000.0
    max_loss_limit_dollars: float = 2_000.0
    profit_target_dollars: float = 3_000.0
    # The Combine has no minimum trading day requirement -- the $150 winning-day
    # rule belongs to the funded stage. A non-zero default invented an objective
    # the firm does not have and would have kept the bot trading after it had
    # already passed.
    min_trading_days: int = 0
    # TopStep's max loss trails the highest END-OF-DAY balance, not intraday
    # equity, and on a funded account it stops trailing once it reaches the
    # starting balance. FTMO's equivalent is static -- this flag is the whole
    # difference and `guardrails` reads it rather than assuming.
    trailing_max_loss: bool = True
    trailing_stops_at_initial_balance: bool = True
    # Internal stop, hit before the firm's limit, so a bad day ends by the bot's
    # choice rather than the firm's. Same idea as the forex internal day cap.
    internal_daily_stop_dollars: float = 400.0
    # Dollars held back above the nearest loss floor and never risked. The
    # remaining-room guard already refuses a trade larger than the room left,
    # but "spend the room down to the last dollar" still lands the account
    # exactly ON the floor, which is a breach. Reserving a buffer is what turns
    # 0.1% of simulated paths failing into 0.000% -- including when the edge is
    # gone entirely, because the bot stands down instead of taking the trade
    # that would end it. Measured: at $0 reserve the flat regime breached 1.08%
    # of paths; at $200 it breached none in 20,000.
    loss_room_reserve_dollars: float = 200.0
    max_consecutive_losses: int = 3
    stop_at_target: bool = True
    # Consistency rule: no single day may be more than this share of total
    # profit when a payout is requested. Verify the current figure; it gates
    # withdrawals rather than the account, so it warns instead of blocking.
    consistency_max_day_share: float = 0.50

    # --- sessions -----------------------------------------------------------
    # COMEX gold trades nearly around the clock but TopStep requires flat before
    # the daily close. Times are US/Central, the exchange's own clock, because
    # that is the clock the rule is written in. VERIFY the deadline for metals
    # specifically -- it is not the same hour as the equity index products, and
    # being late is a rule breach rather than a bad fill.
    exchange_timezone: str = "America/Chicago"
    flat_by_hour: int = 15
    flat_by_minute: int = 10
    session_break_start_hour: int = 16    # 16:00-17:00 CT daily maintenance halt
    session_break_end_hour: int = 17

    # --- news ---------------------------------------------------------------
    news_enabled: bool = True
    news_require_calendar: bool = True
    news_currencies: tuple[str, ...] = ("USD",)
    news_min_impact: str = "high"
    news_minutes_before: int = 5
    news_minutes_after: int = 3

    # --- exit ---------------------------------------------------------------
    # `fixed_tp3` needs one contract; `be_33_33_34` needs three, and unlike lots
    # there is no rounding that can fake a third leg. Below three contracts the
    # split exit is not available at all, which `guardrails` refuses rather than
    # silently degrading to a different system than the lab measured.
    exit_mode: str = "contract_tier"
    split_exit_min_contracts: int = 3

    # --- costs --------------------------------------------------------------
    # Breakeven is only breakeven once the trade has paid for itself. These feed
    # `trader.cost_points`, which is what makes the advertised cost-covered stop
    # actually cover cost. ESTIMATES -- replace with what the account is really
    # charged, per side, per contract.
    commission_per_contract: float = 0.74     # round turn, per contract
    slippage_ticks: float = 1.0               # expected, per exit

    # --- connection ---------------------------------------------------------
    # Credentials never live here. Set FUT_PROJECTX_USERNAME and
    # FUT_PROJECTX_API_KEY in the environment; `broker` reads them directly.
    projectx_base_url: str = "https://api.topstepx.com"
    projectx_user_hub_url: str = "https://rtc.topstepx.com/hubs/user"
    projectx_market_hub_url: str = "https://rtc.topstepx.com/hubs/market"
    account_id: int = 0                  # 0 = resolve the single active account
    request_timeout_seconds: float = 15.0
    write_spacing_seconds: float = 1.0
    reconnect_initial_seconds: float = 5.0
    reconnect_max_seconds: float = 60.0
    max_requests_per_day: int = 20_000

    # --- bookkeeping --------------------------------------------------------
    initial_balance: float = 0.0         # 0 = capture the balance on first run
    tags: tuple[str, ...] = field(default_factory=lambda: ("quantum",))

    def __post_init__(self) -> None:
        errors = []
        finite = {
            "risk_dollars": self.risk_dollars,
            "max_open_risk_dollars": self.max_open_risk_dollars,
            "daily_loss_limit_dollars": self.daily_loss_limit_dollars,
            "max_loss_limit_dollars": self.max_loss_limit_dollars,
            "internal_daily_stop_dollars": self.internal_daily_stop_dollars,
            "profit_target_dollars": self.profit_target_dollars,
            "tick_size": self.tick_size,
            "tick_value": self.tick_value,
        }
        non_finite = [name for name, value in finite.items()
                      if not math.isfinite(float(value))]
        if non_finite:
            errors.append("risk settings must be finite: " + ", ".join(non_finite))
        if not self.contract_symbol.strip():
            errors.append("contract_symbol must not be empty")
        if not self.timeframes:
            errors.append("timeframes must not be empty")
        if self.tick_size <= 0 or self.tick_value <= 0:
            errors.append("tick_size and tick_value must be > 0")
        if self.contract_step < 1 or self.min_contracts < 1:
            errors.append("contract_step and min_contracts must be >= 1")
        if self.max_mini_contracts < 1 or self.max_micro_contracts < 1:
            errors.append("contract caps must be >= 1")
        if self.max_contracts < self.min_contracts:
            errors.append("the contract cap must be >= min_contracts")
        if self.commission_per_contract < 0 or self.slippage_ticks < 0:
            errors.append("commission and slippage must not be negative")
        if self.risk_dollars <= 0:
            errors.append("risk_dollars must be > 0")
        peak_risk = (self.dynamic_risk_max_dollars if self.dynamic_risk_enabled
                     else self.risk_dollars)
        if self.max_open_risk_dollars < peak_risk:
            errors.append("max_open_risk_dollars must be >= the maximum per-setup risk")
        if (self.dynamic_risk_enabled
                and self.dynamic_risk_max_dollars > self.internal_daily_stop_dollars):
            errors.append("dynamic_risk_max_dollars must be <= "
                          "internal_daily_stop_dollars, or the top tier can never "
                          "be taken on the first trade of a day")
        if self.dynamic_risk_enabled:
            tiers = (self.dynamic_risk_max_dollars, self.dynamic_risk_tier2_dollars,
                     self.dynamic_risk_tier3_dollars, self.risk_dollars)
            if not all(value > 0 for value in tiers):
                errors.append("dynamic risk tiers must be positive")
            if not all(a >= b for a, b in zip(tiers, tiers[1:])):
                errors.append("dynamic risk tiers must decrease toward risk_dollars")
            ladder = (self.dynamic_risk_dd1_dollars, self.dynamic_risk_dd2_dollars,
                      self.dynamic_risk_dd3_dollars)
            if not (0 < ladder[0] < ladder[1] < ladder[2]):
                errors.append("dynamic risk drawdown thresholds must increase")
        if any(value <= 0 for value in self.recovery_risk_dollars):
            errors.append("recovery risk tiers must be positive")
        if (self.recovery_risk_dollars
                and max(self.recovery_risk_dollars) >= self.risk_dollars):
            errors.append("recovery risk tiers must be smaller than risk_dollars")
        if self.loss_room_reserve_dollars < 0:
            errors.append("loss_room_reserve_dollars must be >= 0")
        smallest = min([self.risk_dollars, *self.recovery_risk_dollars])
        if self.loss_room_reserve_dollars >= self.max_loss_limit_dollars - smallest:
            errors.append("loss_room_reserve_dollars leaves no room for even the "
                          "smallest risk tier")
        if not 0 < self.internal_daily_stop_dollars <= self.daily_loss_limit_dollars:
            errors.append("internal_daily_stop_dollars must be > 0 and "
                          "<= daily_loss_limit_dollars")
        if self.daily_loss_limit_dollars <= 0 or self.max_loss_limit_dollars <= 0:
            errors.append("daily and max loss limits must be > 0")
        if self.max_open_risk_dollars >= self.daily_loss_limit_dollars:
            errors.append("max_open_risk_dollars must be < daily_loss_limit_dollars: "
                          "every open stop can be hit on the same day")
        if self.daily_loss_limit_dollars > self.max_loss_limit_dollars:
            errors.append("daily_loss_limit_dollars must be <= max_loss_limit_dollars")
        if self.max_concurrent_trades < 1:
            errors.append("max_concurrent_trades must be >= 1")
        if self.max_consecutive_losses < 1:
            errors.append("max_consecutive_losses must be >= 1")
        if self.profit_target_dollars <= 0:
            errors.append("profit_target_dollars must be > 0")
        if self.min_trading_days < 0:
            errors.append("min_trading_days must be >= 0")
        if not 0 < self.consistency_max_day_share <= 1:
            errors.append("consistency_max_day_share must be in (0, 1]")
        if self.exit_mode not in {"fixed_tp3", "be_33_33_34", "contract_tier"}:
            errors.append(f"unsupported exit_mode: {self.exit_mode}")
        if self.split_exit_min_contracts < 3:
            errors.append("split_exit_min_contracts must be >= 3: the split exit "
                          "has three legs and contracts cannot be divided")
        if not 0 <= self.flat_by_hour <= 23 or not 0 <= self.flat_by_minute <= 59:
            errors.append("flat_by_hour/flat_by_minute must be a valid time of day")
        if self.request_timeout_seconds <= 0:
            errors.append("request_timeout_seconds must be > 0")
        if self.write_spacing_seconds < 0:
            errors.append("write_spacing_seconds must be >= 0")
        if self.reconnect_initial_seconds <= 0:
            errors.append("reconnect_initial_seconds must be > 0")
        if self.reconnect_max_seconds < self.reconnect_initial_seconds:
            errors.append("reconnect_max_seconds must be >= reconnect_initial_seconds")
        if self.news_minutes_before < 0 or self.news_minutes_after < 0:
            errors.append("news windows must not be negative")
        if errors:
            raise ValueError("invalid futures settings: " + "; ".join(errors))

    @property
    def is_micro(self) -> bool:
        """Whether the configured root is a micro contract."""
        if self.micro_contract is not None:
            return bool(self.micro_contract)
        root = self.contract_symbol.strip().upper()
        return root.startswith("M") and len(root) >= 3

    @property
    def max_contracts(self) -> int:
        """The simultaneous cap in the unit actually traded."""
        return self.max_micro_contracts if self.is_micro else self.max_mini_contracts

    @property
    def cost_points(self) -> float:
        """Price distance one contract must clear to have paid for itself.

        Commission is a cash amount and the stop is a price, so it is converted
        through `value_per_point`; slippage is already in ticks.
        """
        commission = (self.commission_per_contract / self.value_per_point
                      if self.value_per_point else 0.0)
        return commission + self.slippage_ticks * self.tick_size

    @property
    def value_per_point(self) -> float:
        """Account currency per 1.0 of price, per single contract."""
        return self.tick_value / self.tick_size

    def resolved_exit_mode(self, contracts: int) -> str:
        """Concrete exit policy for a position of this many contracts.

        The forex twin tiers on anchored capital because a lot can be split
        three ways at any balance that sizes 0.03. Contracts cannot: three legs
        need three contracts, full stop, so the tier is on contract count.
        """
        if self.exit_mode != "contract_tier":
            return self.exit_mode
        return ("be_33_33_34" if contracts >= self.split_exit_min_contracts
                else "fixed_tp3")

    def leg_weights_for(self, contracts: int) -> tuple[float, ...]:
        if self.resolved_exit_mode(contracts) == "fixed_tp3":
            return (1.0,)
        return (0.33, 0.33, 0.34)


def _coerce(raw: str, current):
    if isinstance(current, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current, tuple):
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    return type(current)(raw)


def load() -> Settings:
    """Defaults, then settings.local.json, then FUT_* environment variables.

    A key beginning with `_` is a comment and is ignored, so the reason behind a
    number can sit next to it. Any other unknown key still raises: a typo in a
    loss limit must not pass silently.
    """
    values: dict[str, object] = {}
    if LOCAL_SETTINGS.exists():
        values.update({key: value for key, value
                       in json.loads(LOCAL_SETTINGS.read_text(encoding="utf-8")).items()
                       if not key.startswith("_")})
    defaults = Settings()
    for spec in fields(Settings):
        env = os.getenv(f"{ENV_PREFIX}{spec.name.upper()}")
        if env is not None:
            values[spec.name] = _coerce(env, getattr(defaults, spec.name))
    for key in ("timeframes", "tags", "news_currencies"):
        if isinstance(values.get(key), list):
            values[key] = tuple(values[key])
    return Settings(**values)
