"""Reading the work calendar through the Microsoft 365 MCP connector.

The alternative to :mod:`calendar_owa`, and the better one where it is
available. Driving Outlook Web works, but it needs a browser, a corporate SSO
sign-in that must never be retried, and a saved profile that is a replayable
MFA-satisfied session sitting on disk. This path needs none of that: one
delegated read scope, a refresh token, and stdlib HTTP.

The two readers deliberately share a contract -- ``(events, degraded)`` -- so
the classifier cannot tell them apart, and so the rule that matters survives
either way: a read that failed is never allowed to look like a week with no
office days. See COMMON_MISTAKES #8.

Parsing is kept pure and separate from the transport, exactly as the OWA reader
keeps ``parse_event_label`` separate from ``read_week``, so the fragile half is
unit-testable against captured payloads with no network and no sign-in.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import m365_mcp
from .models import CalendarEvent

logger = logging.getLogger(__name__)

CALENDAR_TOOL = "outlook_calendar_search"

#: How far back the query reaches beyond the window being classified.
#:
#: The connector filters on the event's own start, so a fortnight of leave that
#: began before the window would not be returned at all -- and those days would
#: then read as "no booking, therefore home", which claims the working-from-home
#: allowance for days spent on holiday. Over-claiming is the one direction this
#: system is not allowed to fail in, so the query reaches back far enough to see
#: the whole of any plausible absence and the extra days are clipped off after.
LOOKBACK_DAYS = 31

#: A sanity bound on one all-day event, so a corrupt or open-ended entry cannot
#: paint months of the ledger.
MAX_ALL_DAY_SPAN = 60

#: Graph's showAs vocabulary mapped onto the one :class:`CalendarEvent` uses.
#: ``workingElsewhere`` is not an absence -- it is a normal working day that
#: happens to be somewhere else -- so it maps to busy, not oof.
_SHOW_AS = {
    "free": "free",
    "busy": "busy",
    "tentative": "tentative",
    "oof": "oof",
    "workingelsewhere": "busy",
    "unknown": "busy",
}

#: Graph normally answers in UTC, but a mailbox can be configured to answer in
#: its own zone, and it names that zone the Windows way rather than the IANA
#: way. Only the zones this tenant could plausibly produce are listed; an
#: unknown one falls back to UTC with a warning rather than failing the run,
#: because the zone only decides which local day a *timed* event lands on.
#: All-day events -- which is how leave and desk bookings both arrive -- never
#: consult it at all.
_WINDOWS_ZONES = {
    "utc": "UTC",
    "w. europe standard time": "Europe/Berlin",
    "romance standard time": "Europe/Paris",
    "gmt standard time": "Europe/London",
    "central europe standard time": "Europe/Budapest",
    "central european standard time": "Europe/Warsaw",
}


class CalendarUnavailable(RuntimeError):
    """The calendar could not be read. Never to be confused with an empty one."""


def _zone(name: str, *, default: ZoneInfo) -> ZoneInfo:
    if not name:
        return default
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        pass
    mapped = _WINDOWS_ZONES.get(name.strip().lower())
    if mapped:
        try:
            return ZoneInfo(mapped)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            pass
    logger.warning("m365 calendar: unknown time zone %r; reading it as UTC", name)
    return ZoneInfo("UTC")


def _wall_clock(value: str) -> datetime:
    """Parse Graph's wall-clock string, which carries seven fractional digits.

    ``fromisoformat`` accepts three or six, not seven, so the fraction is
    dropped: nothing here is decided at sub-second resolution.
    """
    text = value.strip().rstrip("Z")
    if "." in text:
        text = text.split(".", 1)[0]
    return datetime.fromisoformat(text)


def parse_event(raw: dict, *, tz: ZoneInfo) -> list[CalendarEvent]:
    """Turn one connector event into the days it covers.

    A list, not a single event, because an all-day entry is a half-open range:
    leave booked Monday to Friday arrives as one object running from Monday
    00:00 to Saturday 00:00 and has to become five days. Taking only the start
    date -- the obvious reading -- would mark Monday absent and leave Tuesday to
    Friday looking like ordinary working-from-home days to claim for.

    An all-day event's dates are read literally and never converted. A day-long
    entry on the 3rd is on the 3rd in every zone, and converting its midnight
    bounds through a zone that is behind the local one moves it to the 2nd.
    """
    if raw.get("isCancelled"):
        return []

    start_raw = raw.get("start") or {}
    end_raw = raw.get("end") or {}
    if not start_raw.get("dateTime"):
        raise ValueError(f"event without a start: {sorted(raw)}")

    subject = (raw.get("subject") or "").strip()
    organiser = (raw.get("organizer") or "").strip()
    show_as = _SHOW_AS.get((raw.get("showAs") or "busy").strip().lower(), "busy")
    all_day = bool(raw.get("isAllDay"))

    def event(day: date) -> CalendarEvent:
        return CalendarEvent(
            day=day,
            subject=subject,
            all_day=all_day,
            show_as=show_as,
            organiser=organiser,
        )

    if not all_day:
        moment = _wall_clock(start_raw["dateTime"]).replace(
            tzinfo=_zone(start_raw.get("timeZone", ""), default=ZoneInfo("UTC"))
        )
        return [event(moment.astimezone(tz).date())]

    first = _wall_clock(start_raw["dateTime"]).date()
    last = _wall_clock(end_raw["dateTime"]).date() if end_raw.get("dateTime") else first
    span = max(1, min((last - first).days, MAX_ALL_DAY_SPAN))  # end is exclusive
    return [event(first + timedelta(days=offset)) for offset in range(span)]


def read_range(
    start: date,
    end: date,
    *,
    tz: str = "Europe/Amsterdam",
    require_events: bool = True,
    search: Callable[..., m365_mcp.SearchResult] = m365_mcp.search,
) -> tuple[list[CalendarEvent], bool]:
    """Read ``start..end`` inclusive. Returns ``(events, degraded)``.

    ``search`` is injected so the whole of this can be tested against captured
    payloads without a token or a network.

    ``require_events`` is the deliberate paranoid half. Every failure the
    connector reports is caught and turned into degraded, but a wildcard search
    that quietly stopped matching would return a clean, empty, entirely
    plausible answer -- and a week of "no bookings" becomes a week of
    working-from-home claims. A working window with no calendar entries at all
    is therefore treated as a question for a human rather than as evidence.
    Being asked about a genuinely quiet week costs one Slack click; the other
    way round costs a false expense claim.
    """
    zone = ZoneInfo(tz)
    window_start = start - timedelta(days=LOOKBACK_DAYS)
    window_end = end + timedelta(days=1)

    try:
        found = search(
            CALENDAR_TOOL,
            query="*",
            # Anchors the search to the date range instead of relevance-ranking
            # it, which is the difference between "every event in the window"
            # and "the 25 events the ranker liked most".
            order="oldest",
            afterDateTime=f"{window_start.isoformat()}T00:00:00",
            beforeDateTime=f"{window_end.isoformat()}T00:00:00",
        )
    except m365_mcp.M365Error as exc:
        logger.error("m365 calendar: %s -- recording the window as degraded", exc)
        return [], True

    if not found.complete:
        logger.error("m365 calendar: the search returned a partial answer; treating as degraded")
        return [], True

    events: list[CalendarEvent] = []
    failures = 0
    for raw in found.items:
        try:
            events.extend(parse_event(raw, tz=zone))
        except (ValueError, KeyError, TypeError) as exc:
            failures += 1
            logger.warning("m365 calendar: unparseable event (%s)", exc)

    # Every event failing to parse means the payload moved, not that the
    # calendar is empty. Same rule as the OWA reader, same reason.
    if found.items and failures == len(found.items):
        logger.error(
            "m365 calendar: %d events returned, none parsed -- the payload may have changed",
            failures,
        )
        return [], True

    in_range = [e for e in events if start <= e.day <= end]

    if require_events and not in_range:
        logger.error(
            "m365 calendar: not one event between %s and %s; refusing to read that as "
            "'no office days'",
            start,
            end,
        )
        return [], True

    logger.info(
        "m365 calendar: %s..%s -> %d events (%d returned, %d unparsed)",
        start,
        end,
        len(in_range),
        len(found.items),
        failures,
    )
    return in_range, False
