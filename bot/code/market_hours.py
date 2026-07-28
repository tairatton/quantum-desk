"""When the market next shuts, and whether a new entry may still be opened.

FTMO treats opening a position within two hours of a closure lasting two hours or
more as gap trading. The weekly close always qualifies; so do Christmas, New
Year and the early closes around them, when gold stops for anything from a few
hours to four days.

MetaTrader5 5.0.5735 — the build on this machine — exposes no session functions
(`symbol_info_sessions_quote` and `_trade` are both absent), so the schedule
cannot be read from the terminal the way `bot.news` cannot read the calendar
from it either. Closures therefore come from two places:

  * the recurring weekly close, from settings
  * `market_closures`, a list of one-off closures kept in settings

Neither is guessed at silently. `observed_weekly_close` measures the real close
from bar history so a wrong configuration is visible in `--status` instead of
being discovered by an order that should never have been sent.

Positions are never closed to avoid a closure. Holding gold over a weekend is
what the backtest does — a 120-bar M30 timeout is 60 hours — and forcing an exit
to dodge the gap would be a different, unmeasured exit policy. Only *opening* is
blocked.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

import pandas as pd


@dataclass(frozen=True)
class Closure:
    """A period the market is shut, on the broker's server clock."""
    start: datetime
    end: datetime
    label: str

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


def _parse_closure(row) -> Closure | None:
    """Read one configured closure.

    Two shapes are accepted, because both are things a person actually needs to
    write down:

      "2026-12-25"                          a whole day shut
      ["2026-12-24 20:00", "2026-12-26 01:00", "Christmas"]   an exact span
    """
    try:
        if isinstance(row, str):
            day = datetime.fromisoformat(row).date()
            return Closure(datetime.combine(day, time.min),
                           datetime.combine(day, time.max), f"closed {day.isoformat()}")
        start = datetime.fromisoformat(str(row[0]))
        end = datetime.fromisoformat(str(row[1]))
        label = str(row[2]) if len(row) > 2 else "scheduled closure"
        if end <= start:
            return None
        return Closure(start, end, label)
    except (ValueError, TypeError, IndexError):
        return None


def configured_closures(settings) -> list[Closure]:
    """One-off closures from settings, bad rows dropped rather than fatal."""
    found = [_parse_closure(row) for row in settings.market_closures]
    return sorted((closure for closure in found if closure is not None),
                  key=lambda closure: closure.start)


def _weekly_span(settings, close_day: datetime) -> Closure:
    start = close_day.replace(hour=settings.weekly_close_hour,
                              minute=0, second=0, microsecond=0)
    reopen_days = (settings.weekly_open_weekday - start.weekday()) % 7 or 7
    end = (start + timedelta(days=reopen_days)).replace(
        hour=settings.weekly_open_hour, minute=0, second=0, microsecond=0)
    return Closure(start, end, "weekly close")


def weekly_close(settings, after: datetime) -> Closure:
    """The weekly closure that has not finished yet, on the server clock.

    Looking only forwards would skip the closure already under way: asked on a
    Saturday, "the next Friday" is six days out, and the weekend the market is
    actually sitting in gets missed entirely. So the most recent close is checked
    first and returned when it has not reopened yet.
    """
    days_back = (after.weekday() - settings.weekly_close_weekday) % 7
    previous = after - timedelta(days=days_back)
    current = _weekly_span(settings, previous)
    if current.start > after:
        current = _weekly_span(settings, previous - timedelta(days=7))
    if current.end > after:
        return current
    return _weekly_span(settings, current.start + timedelta(days=7))


def next_closure(settings, server_time: datetime) -> Closure:
    """The soonest closure that has not finished yet.

    A closure already under way is returned as itself, so a caller can tell
    "shut right now" from "shutting soon" by comparing against `start`.
    """
    candidates = [weekly_close(settings, server_time)]
    candidates += [closure for closure in configured_closures(settings)
                   if closure.end > server_time]
    return min(candidates, key=lambda closure: closure.start)


def is_closed(settings, server_time: datetime) -> Closure | None:
    """The closure containing `server_time`, if the market is shut."""
    closure = next_closure(settings, server_time)
    return closure if closure.start <= server_time < closure.end else None


def entry_blackout(settings, server_time: datetime) -> Closure | None:
    """The closure whose run-up bars new entries, if any.

    A closure shorter than FTMO's two-hour threshold is not gap trading and is
    left alone: blocking it would cost trades the rules allow.
    """
    closure = next_closure(settings, server_time)
    if closure.hours < settings.min_closure_hours_for_gap_rule:
        return None
    opens_at = closure.start - timedelta(hours=settings.blackout_hours_before_close)
    return closure if opens_at <= server_time < closure.end else None


def observed_week_end(frame: pd.DataFrame) -> tuple[int, time] | None:
    """(weekday, time) the week's final bar closes, measured from real bars.

    `weekly_close_weekday` and `weekly_close_hour` are written-down numbers and
    brokers do not agree on them: the same instrument ends Friday 21:00 on a UTC+0
    server and Saturday 00:00 on a UTC+3 one. Measuring turns a wrong setting into
    something visible rather than something discovered by an order that should
    never have been sent.

    This deliberately does not take the configured weekday as a hint. An earlier
    version did, and it went silent in exactly the case worth catching — a close
    that lands at 00:00 means the last bars sit on the *previous* weekday, so
    filtering to the configured day found nothing and reported nothing.

    The modal week-end is used, so one holiday-shortened week cannot redefine the
    schedule.
    """
    if frame is None or len(frame) < 2:
        return None
    stamps = pd.to_datetime(frame["time"])
    step = stamps.diff().median()
    if pd.isna(step) or step <= pd.Timedelta(0):
        return None
    weeks = stamps.dt.isocalendar()
    # A bar's timestamp is its open; the week ends when the last one closes.
    ends = stamps.groupby([weeks["year"], weeks["week"]]).max() + step
    if len(ends) < 2:
        return None
    # Drop the newest week: it is still in progress and would read as an early close.
    ends = ends.iloc[:-1]
    if ends.empty:
        return None
    counts = ends.apply(lambda moment: (moment.weekday(), moment.time())).value_counts()
    return counts.index[0]
