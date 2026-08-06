"""Economic calendar and the news blackout windows built from it.

The MetaTrader5 Python package on this machine (5.0.5735) exposes no calendar
functions, so the events come from a public weekly JSON feed and are cached on
disk. Two rules make this necessary rather than optional:

  * a funded FTMO Standard account cannot open, close or place a pending order
    within 2 minutes either side of selected releases
  * "gap trading" — opening around major news — is a forbidden practice in every
    phase

The default window is the 2 minutes FTMO actually requires. Widening it is a
trading decision, not a rule: the backtest traded straight through news, so a
wider blackout makes live results diverge from the measured edge on purpose.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from typing import TYPE_CHECKING

if TYPE_CHECKING:                       # a venue's Settings, never imported at runtime
    from bot.settings import Settings

# The high-impact USD calendar is the same feed whichever venue is trading it, so
# the cache is shared rather than duplicated per instance: one fetch, one file,
# and no chance of the forex bot standing down for an event the futures bot
# never saw. Instance-specific files (state, journal, lock) stay per instance.
CACHE_PATH = Path(__file__).resolve().parent / "news_cache.json"
IMPACT_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class Event:
    title: str
    currency: str
    impact: str
    at_utc: datetime

    def window(self, offset_hours: float, before: int, after: int
               ) -> tuple[datetime, datetime]:
        """Blackout window expressed on the broker's server clock."""
        server = self.at_utc.replace(tzinfo=None) + timedelta(hours=offset_hours)
        return server - timedelta(minutes=before), server + timedelta(minutes=after)


@dataclass(frozen=True)
class Calendar:
    events: tuple[Event, ...]
    fetched_at: datetime | None
    source: str            # "network" | "cache" | "stale-cache" | "manual" | "none"
    error: str = ""

    @property
    def age_hours(self) -> float | None:
        if self.fetched_at is None:
            return None
        return (datetime.now() - self.fetched_at).total_seconds() / 3600

    @property
    def usable(self) -> bool:
        """A calendar is only usable if it actually carries events.

        Reaching the feed is not the same as understanding it. An empty list, or
        a schema change that makes every row unparseable, would otherwise arrive
        as source="network" with no events and count as a working guard — which
        reads as "no news today" and silently removes the blackout entirely.
        """
        return bool(self.events) and self.source in {"network", "cache", "manual"}


def _parse(payload: list[dict]) -> list[Event]:
    events = []
    for row in payload:
        stamp = row.get("date")
        if not stamp:
            continue
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        events.append(Event(title=str(row.get("title", "?")),
                            currency=str(row.get("country", "")).upper(),
                            impact=str(row.get("impact", "")).lower(),
                            at_utc=moment.astimezone(timezone.utc)))
    return events


def fetch(settings: Settings, timeout: int = 15) -> Calendar:
    """Download the weekly calendar and cache it. Never raises."""
    request = urllib.request.Request(
        settings.news_source_url,
        headers={"User-Agent": "xau-analysis-bot/1.0 (personal FTMO risk guard)"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        return Calendar((), None, "none", f"fetch failed: {error}")
    events = _parse(payload)
    if not events:
        # Reached the feed and understood nothing in it: an empty week, or the
        # schema moved. Either way it must not be cached over a good calendar or
        # reported as a working guard.
        return Calendar((), None, "none",
                        f"feed returned {len(payload)} rows but no usable events; "
                        f"check {settings.news_source_url}")
    CACHE_PATH.write_text(json.dumps(
        {"fetched_at": datetime.now().isoformat(timespec="seconds"), "events": payload},
        ensure_ascii=False), encoding="utf-8")
    return Calendar(tuple(events), datetime.now(), "network")


def from_cache() -> Calendar:
    if not CACHE_PATH.exists():
        return Calendar((), None, "none", "no cache on disk")
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return Calendar(tuple(_parse(raw["events"])),
                        datetime.fromisoformat(raw["fetched_at"]), "cache")
    except (ValueError, KeyError, OSError) as error:
        return Calendar((), None, "none", f"unreadable cache: {error}")


def manual(settings: Settings) -> list[Event]:
    """Events typed straight into settings, already on the server clock."""
    out = []
    for stamp in settings.news_times:
        try:
            out.append(Event("manual", "MANUAL", "high",
                             datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)))
        except ValueError:
            continue
    return out


def load(settings: Settings) -> Calendar:
    """Cache when fresh, network when stale, cache again when the network fails."""
    if not settings.news_enabled:
        return Calendar(tuple(manual(settings)), None, "manual")
    cached = from_cache()
    if cached.usable and (cached.age_hours or 0) < settings.news_cache_hours:
        return cached
    fresh = fetch(settings)
    if fresh.usable:
        return fresh
    if cached.usable:
        # Keep the events for operator visibility, but do not call an expired
        # calendar usable. With news_require_calendar enabled, the entry guard
        # must fail closed when refresh fails instead of trading indefinitely
        # from last week's cached schedule.
        return Calendar(cached.events, cached.fetched_at, "stale-cache",
                        f"using stale cache; {fresh.error}")
    return fresh


def relevant(calendar: Calendar, settings: Settings) -> list[Event]:
    floor = IMPACT_ORDER.get(settings.news_min_impact.lower(), 2)
    wanted = {code.upper() for code in settings.news_currencies}
    return [event for event in calendar.events
            if IMPACT_ORDER.get(event.impact, 0) >= floor
            and (not wanted or event.currency in wanted or event.currency == "MANUAL")]


def windows(settings: Settings, offset_hours: float,
            calendar: Calendar | None = None) -> list[tuple[datetime, datetime]]:
    """Blackout windows on the server clock, manual entries included."""
    calendar = calendar if calendar is not None else load(settings)
    spans = [event.window(offset_hours, settings.news_minutes_before,
                          settings.news_minutes_after)
             for event in relevant(calendar, settings) if event.currency != "MANUAL"]
    # Manual entries are typed in server time already, so they need no shift.
    spans += [event.window(0.0, settings.news_minutes_before,
                           settings.news_minutes_after)
              for event in manual(settings)]
    return sorted(spans)


def next_event(settings: Settings, server_time: datetime, offset_hours: float,
               calendar: Calendar | None = None) -> tuple[Event, datetime] | None:
    """The soonest upcoming relevant event, for logging in --status."""
    calendar = calendar if calendar is not None else load(settings)
    upcoming = []
    for event in relevant(calendar, settings):
        shift = 0.0 if event.currency == "MANUAL" else offset_hours
        moment = event.at_utc.replace(tzinfo=None) + timedelta(hours=shift)
        if moment >= server_time:
            upcoming.append((moment, event))
    if not upcoming:
        return None
    moment, event = min(upcoming, key=lambda pair: pair[0])
    return event, moment
