"""Reading the work calendar out of Outlook Web.

Why scrape rather than call an API: this tenant does not grant app
registrations, so Microsoft Graph is unavailable. The alternatives were a
published-ICS capability URL (world-readable, and it silently truncates to a
rolling three-month window) or borrowing the access token OWA mints in-page for
its own first-party client -- which is the token-replay shape security tooling
is built to flag. Driving the page as the signed-in user is the honest option
and reuses the Entra session the job already holds.

OWA puts the whole event description in each element's ``aria-label``:

    Booking (A / Desk 1.01 / Zone A), all day event, Monday, August 31, 2026, By deskbird, Free
    Daily standup, 8:45 AM to 9:30 AM, Monday, August 31, 2026, By A Colleague, Busy, Recurring event

Parsing is kept separate from the browser so the fragile half is unit-testable
against captured strings, with no login required.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import date, timedelta

from .entra import on_microsoft_login
from .models import CalendarEvent

logger = logging.getLogger(__name__)

_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        start=1,
    )
}

_DATE_RE = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2}),\s*(\d{4})\b", re.IGNORECASE)
_ORGANISER_RE = re.compile(r",\s*By\s+([^,]+)")
_SHOW_AS_RE = re.compile(r",\s*(Free|Busy|Tentative|Away|Working elsewhere)\b", re.IGNORECASE)
_ALL_DAY_RE = re.compile(r",\s*all day event\b", re.IGNORECASE)
_TIME_RE = re.compile(r",\s*\d{1,2}:\d{2}\s*(?:AM|PM)?\s+to\s+", re.IGNORECASE)

_SHOW_AS_MAP = {
    "free": "free",
    "busy": "busy",
    "tentative": "tentative",
    "away": "oof",
    "working elsewhere": "busy",
}


class LabelParseError(ValueError):
    """An aria-label did not look like an event."""


def parse_event_label(label: str) -> CalendarEvent:
    """Turn one OWA ``aria-label`` into a :class:`CalendarEvent`.

    Parsed from the right-hand side. The subject is whatever precedes the
    time-or-all-day marker, because a subject may legitimately contain commas
    and splitting on them loses events with punctuation in the title.
    """
    m = _DATE_RE.search(label)
    if not m:
        raise LabelParseError(f"no date found in {label[:80]!r}")
    day = date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))

    all_day = bool(_ALL_DAY_RE.search(label))
    marker = _ALL_DAY_RE.search(label) or _TIME_RE.search(label)
    subject = label[: marker.start()].strip() if marker else label[: m.start()].rstrip(", ").strip()

    organiser_m = _ORGANISER_RE.search(label)
    show_as_m = _SHOW_AS_RE.search(label)
    show_as = _SHOW_AS_MAP.get(show_as_m.group(1).lower(), "busy") if show_as_m else "busy"

    return CalendarEvent(
        day=day,
        subject=subject,
        all_day=all_day,
        show_as=show_as,
        organiser=organiser_m.group(1).strip() if organiser_m else "",
    )


#: Collects every element carrying a descriptive aria-label. OWA renders events
#: as buttons/gridcells; harvesting broadly and filtering by parseability is
#: more durable than depending on one component's class name.
_HARVEST_JS = """() => {
  const out = new Set();
  for (const el of document.querySelectorAll('[role="button"],[role="gridcell"],[aria-label]')) {
    const a = el.getAttribute('aria-label') || '';
    if (a.length > 18 && /\\d{4}/.test(a)) out.add(a);
  }
  return [...out];
}"""


#: The date-range header, e.g. "August 31 - September 4, 2026, Jump to a
#: specific date or date range." It is the only reliable statement of which week
#: is on screen: a week with no events yields no event labels at all, so the
#: rendered range cannot be inferred from the events themselves.
_RANGE_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})\s*[\u2013\u2014-]\s*"
    r"(?:(" + "|".join(_MONTHS) + r")\s+)?(\d{1,2}),\s*(\d{4})",
    re.IGNORECASE,
)

WORKWEEK_URL = "https://outlook.office.com/calendar/view/workweek"


#: The date-picker header carries this phrase; the Previous/Next week buttons do
#: not. Both kinds of label contain a date RANGE, and the navigation buttons
#: describe the week they would move TO -- so matching the first range on the
#: page silently reads the previous week's dates as the current ones, and every
#: navigation decision is then made against a baseline one week out.
_HEADER_HINT = re.compile(r"jump to a specific date", re.IGNORECASE)
_NAV_HINT = re.compile(r"^\s*go to (previous|next)", re.IGNORECASE)


def rendered_week(page, *, retries: int = 1, pause_ms: int = 3_000) -> tuple[date, date] | None:
    """The week currently on screen, read from the date-picker header.

    Retries because the header is re-rendered asynchronously after a week
    change: reading it immediately after clicking Previous/Next can catch the
    page mid-update with no range present at all. Returning None there is safe
    -- the caller reports degraded rather than guessing a week -- but it costs a
    whole classification run, so give it a moment first.
    """
    for attempt in range(retries + 1):
        found = _rendered_week_once(page)
        if found is not None:
            return found
        if attempt < retries:
            page.wait_for_timeout(pause_ms)
    return None


def _rendered_week_once(page) -> tuple[date, date] | None:
    labels = page.evaluate(_HARVEST_JS)
    # Preferred: the header itself. Fallback: any range that is not a
    # navigation button. Never just "the first range on the page".
    for predicate in (
        lambda x: _HEADER_HINT.search(x),
        lambda x: not _NAV_HINT.search(x),
    ):
        for label in labels:
            if not predicate(label):
                continue
            m = _RANGE_RE.search(label)
            if not m:
                continue
            m1, d1, m2, d2, year = m.groups()
            start = date(int(year), _MONTHS[m1.lower()], int(d1))
            end = date(int(year), _MONTHS[(m2 or m1).lower()], int(d2))
            return start, end
    return None


#: An event label always names an organiser and says when it happens. The page
#: chrome (navigation buttons, the date-range header) never does, which makes
#: this a reliable "the grid has rendered" signal.
_EVENT_HINT = re.compile(r",\s*By\s+[^,]+,", re.IGNORECASE)


def looks_like_event(label: str) -> bool:
    return bool(_EVENT_HINT.search(label)) and (
        "all day event" in label.lower() or _TIME_RE.search(label) is not None
    )


#: Where the page is and what language it speaks. A sign-in pane, Outlook in
#: another language and a changed Outlook all harvest no usable label, and only
#: this tells them apart.
_PAGE_JS = """() => [location.hostname, location.pathname, document.title,
  document.documentElement.lang]"""


def _describe(page, labels: list[str]) -> str:
    """The page a week was read from, described without a word of calendar content."""
    try:
        host, path, title, lang = page.evaluate(_PAGE_JS)
    except Exception:  # noqa: BLE001 - this only decorates a failure already being logged
        host = path = title = lang = "?"
    events = sum(looks_like_event(x) for x in labels)
    english = sum(bool(_DATE_RE.search(x) or _RANGE_RE.search(x)) for x in labels)
    # The title's first part names the view; the rest names the account.
    return (
        f"page {host}{path}, title {str(title).split(' - ')[0][:60]!r}, lang {lang!r}, "
        f"{len(labels)} labels, {events} like events, {english} with English dates"
    )


#: Words that describe a label's structure rather than its content. Everything
#: else, subjects and people's names included, is written as ``w``.
_SHAPE_WORDS = {
    *_MONTHS,
    *("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"),
    *("am", "pm", "to", "from", "all", "day", "event", "by", "me", "recurring"),
    *("free", "busy", "tentative", "away", "working", "elsewhere", "canceled", "cancelled"),
    *("today", "tomorrow", "yesterday", "jump", "specific", "date", "range", "go", "previous"),
    *("next", "week"),
}
_TOKEN_RE = re.compile(r"[^\W\d_]+|\d+")
_WORD_RUN_RE = re.compile(r"\bw(?:\s+w\b)+")


def _shape_token(m: re.Match) -> str:
    tok = m.group(0)
    if tok.isdigit():
        return "9" * min(len(tok), 4)
    return tok if tok.lower() in _SHAPE_WORDS else "w"


def label_shape(label: str, limit: int = 100) -> str:
    """A label's structure without its content, e.g. ``w, 9:99 AM to 9:99 AM, Monday, ...``."""
    shape = _WORD_RUN_RE.sub("w", _TOKEN_RE.sub(_shape_token, label))
    return " ".join(shape.split())[:limit]


def _shapes(labels: list[str], most: int = 5) -> list[str]:
    return sorted({label_shape(x) for x in labels})[:most]


def _settle(page, *, settle_ms: int, tries: int = 8) -> list[str]:
    """Poll until the event grid has actually rendered.

    Waiting for the label count to stop changing is not enough: the chrome
    settles several seconds before the events do, so a stability check returns
    the navigation buttons alone and the week reads as empty. Waiting for a
    label that actually looks like an event is the honest signal. A week that
    genuinely has no events falls through to the deadline, which is why the
    caller still separates "no events" from "did not load".
    """
    labels: list[str] = []
    for _ in range(tries):
        page.wait_for_timeout(settle_ms // 4)
        labels = page.evaluate(_HARVEST_JS)
        if any(looks_like_event(x) for x in labels):
            page.wait_for_timeout(settle_ms // 4)
            return page.evaluate(_HARVEST_JS)
    return labels


def read_week(
    page,
    monday: date,
    *,
    settle_ms: int = 12_000,
    max_steps: int = 10,
    answer_sign_in: Callable[[object], object] | None = None,
):
    """Read one work week from OWA. Returns ``(events, degraded)``.

    Navigation is by clicking the calendar's own Previous/Next week control.
    The obvious approach -- deep-linking to ``/workweek?startdate=YYYY-MM-DD``
    or ``/workweek/YYYY/MM/DD`` -- silently renders the CURRENT week instead,
    with no error and a full set of event labels. A caller that trusted it read
    this week's events, found none dated in the week it asked for, and
    classified those days as working-from-home. That is the exact shape of
    "missing data became a claim", so the rendered week is now verified against
    the requested one and a mismatch is reported as degraded, never as an empty
    week.

    Outlook sends a browser it has no session for to Entra from the page itself,
    after the load has finished, so a sign-in it wants shows only once the page
    has settled. ``answer_sign_in`` completes it. Without one the week reads as
    degraded, and the log names the sign-in page.
    """
    page.goto(WORKWEEK_URL, wait_until="domcontentloaded", timeout=90_000)
    labels = _settle(page, settle_ms=settle_ms)
    if answer_sign_in is not None and on_microsoft_login(page):
        logger.info("owa: Outlook asked for a sign-in (%s); signing in", _describe(page, labels))
        answer_sign_in(page)
        page.goto(WORKWEEK_URL, wait_until="domcontentloaded", timeout=90_000)
        labels = _settle(page, settle_ms=settle_ms)
    if not any(looks_like_event(x) for x in labels):
        # On a freshly created profile the calendar chrome renders but the event
        # grid stays empty however long it is given. One reload populates it.
        # Without this a cold run -- which is every run in the cluster, because
        # the pod's profile is ephemeral -- sees an empty calendar and would
        # classify the whole window as working-from-home.
        logger.info("owa: no events after first load (%s); reloading once", _describe(page, labels))
        page.reload(wait_until="domcontentloaded", timeout=90_000)
        _settle(page, settle_ms=settle_ms)

    target_end = monday + timedelta(days=4)
    reloaded = False
    for _ in range(max_steps + 1):
        current = rendered_week(page)
        if current is None:
            seen = page.evaluate(_HARVEST_JS)
            header = [x for x in seen if _HEADER_HINT.search(x)]
            if reloaded:
                logger.error(
                    "owa: cannot read the date-range header %s (%s); refusing to guess the week",
                    header,
                    _describe(page, seen),
                )
                return [], True
            # The grid can render while the header does not, and a fresh load brings it back.
            logger.warning(
                "owa: cannot read the date-range header %s (%s); reloading once",
                header,
                _describe(page, seen),
            )
            page.reload(wait_until="domcontentloaded", timeout=90_000)
            _settle(page, settle_ms=settle_ms)
            reloaded = True
            continue
        if current[0] <= monday and target_end <= current[1] + timedelta(days=2):
            break
        direction = "Previous week" if monday < current[0] else "Next week"
        try:
            page.get_by_role("button", name=direction).first.click(timeout=15_000)
        except Exception:  # noqa: BLE001
            logger.error("owa: no %r control; cannot reach the week of %s", direction, monday)
            return [], True
        _settle(page, settle_ms=settle_ms)
    else:
        logger.error("owa: could not reach the week of %s within %d steps", monday, max_steps)
        return [], True

    labels = _settle(page, settle_ms=settle_ms)
    events, parsed, unparsed = [], [], []
    for label in labels:
        try:
            events.append(parse_event_label(label))
            parsed.append(label)
        except LabelParseError:
            unparsed.append(label)

    week = {monday + timedelta(days=i) for i in range(7)}
    in_week = [e for e in events if e.day in week]

    # Every harvested label failing to parse means the label format moved, not
    # that the week was empty.
    degraded = bool(labels) and len(unparsed) == len(labels)
    if degraded:
        logger.error(
            "owa: %d labels harvested, none parsed -- label format may have changed (%s)",
            len(unparsed),
            _describe(page, labels),
        )

    # Parsing is deliberately permissive: a stray label read as an event cannot
    # make a claim, and a strict parser that dropped an absence could. Shapes
    # say what the strays and the unparsed labels were without their content.
    stray = [x for x in parsed if not looks_like_event(x)]
    ranges = [x for x in unparsed if _RANGE_RE.search(x)]
    eventlike = [x for x in unparsed if x not in ranges and looks_like_event(x)]
    other = [x for x in unparsed if x not in ranges and x not in eventlike]
    if eventlike:
        logger.warning(
            "owa: week %s: %d labels look like events but carry no date it can read %s",
            monday,
            len(eventlike),
            _shapes(eventlike),
        )
    logger.info(
        "owa: week %s (rendered %s..%s) -> %d events (%d not event-shaped %s); "
        "unparsed: %d date ranges, %d event-like, %d other %s (%s)",
        monday,
        *current,
        len(in_week),
        len(stray),
        _shapes(stray),
        len(ranges),
        len(eventlike),
        len(other),
        _shapes(other),
        _describe(page, labels),
    )
    return in_week, degraded
