"""The MCP calendar reader, against captured connector payloads.

No network, no token, no browser. The payload shapes here were taken from live
``outlook_calendar_search`` responses and then stripped of anything
identifying, so a change in the connector's output is caught by a test rather
than by a wrong expense claim.
"""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from afas_declaraties import m365_mcp
from afas_declaraties.calendar_mcp import (
    MAX_ALL_DAY_SPAN,
    parse_event,
    read_range,
)
from afas_declaraties.classify import ClassifierConfig, classify_day
from afas_declaraties.models import Reason, Verdict

AMS = ZoneInfo("Europe/Amsterdam")


def meeting(day="2026-09-01", start="06:45:00", end="07:30:00", **over) -> dict:
    raw = {
        "uri": f"calendar:///events/{day}-{start}",
        "id": f"{day}-{start}",
        "subject": "Daily standup",
        "organizer": "a.colleague@<PLACEHOLDER>.invalid",
        "start": {"dateTime": f"{day}T{start}.0000000", "timeZone": "UTC"},
        "end": {"dateTime": f"{day}T{end}.0000000", "timeZone": "UTC"},
        "showAs": "busy",
        "isAllDay": False,
        "isCancelled": False,
    }
    raw.update(over)
    return raw


def all_day(first="2026-09-03", after="2026-09-04", **over) -> dict:
    raw = {
        "uri": f"calendar:///events/{first}-allday",
        "id": f"{first}-allday",
        "subject": "Booking (C / Desk 5.15 / Corners)",
        # .invalid is reserved and can never be a real address; the local
        # part is irrelevant, the substring "deskbird" is what matches.
        "organizer": "notifications@deskbird.invalid",
        "start": {"dateTime": f"{first}T00:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": f"{after}T00:00:00.0000000", "timeZone": "UTC"},
        "showAs": "free",
        "isAllDay": True,
        "isCancelled": False,
    }
    raw.update(over)
    return raw


def fake_search(items, *, notes=(), truncated=False):
    def _search(_tool, **_kwargs):
        return m365_mcp.SearchResult(
            items=list(items), notes=list(notes), total=len(items), truncated=truncated
        )

    return _search


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def test_a_timed_event_lands_on_its_local_day():
    (event,) = parse_event(meeting(), tz=AMS)
    assert event.day == date(2026, 9, 1)
    assert event.subject == "Daily standup"
    assert event.show_as == "busy"
    assert event.all_day is False


def test_a_late_utc_event_belongs_to_the_next_local_day():
    """22:30 UTC is 00:30 in Amsterdam, which is tomorrow. Taking the UTC date
    would file the evening's events under the wrong day."""
    (event,) = parse_event(meeting(day="2026-09-01", start="22:30:00", end="23:00:00"), tz=AMS)
    assert event.day == date(2026, 9, 2)


def test_a_cancelled_event_is_not_an_event():
    assert parse_event(meeting(isCancelled=True), tz=AMS) == []


def test_a_one_day_all_day_event_covers_exactly_that_day():
    """The end is exclusive: 3 Sept 00:00 to 4 Sept 00:00 is one day, not two."""
    days = [e.day for e in parse_event(all_day(), tz=AMS)]
    assert days == [date(2026, 9, 3)]


def test_multi_day_leave_covers_every_day_it_spans():
    """The expensive one. A week of leave arrives as a single object running to
    the following Monday; reading only its start would leave Tuesday to Friday
    looking like ordinary days to claim the home allowance for."""
    leave = all_day(
        first="2026-09-07",
        after="2026-09-12",
        subject="Leave / Verlof",
        showAs="oof",
        organizer="hr@<PLACEHOLDER>.invalid",
    )
    days = [e.day for e in parse_event(leave, tz=AMS)]
    assert days == [date(2026, 9, d) for d in (7, 8, 9, 10, 11)]
    assert all(e.is_out_of_office for e in parse_event(leave, tz=AMS))


def test_an_all_day_event_is_never_shifted_by_a_time_zone():
    """A day-long entry on the 3rd is on the 3rd everywhere. Converting its
    midnight bounds out of a zone behind the local one would move it to the
    2nd, and silently misdate every desk booking."""
    booking = all_day()
    booking["start"]["timeZone"] = "Pacific Standard Time"
    booking["end"]["timeZone"] = "Pacific Standard Time"
    assert [e.day for e in parse_event(booking, tz=AMS)] == [date(2026, 9, 3)]


def test_an_open_ended_all_day_event_cannot_paint_the_ledger():
    runaway = all_day(first="2026-01-01", after="2030-01-01")
    assert len(parse_event(runaway, tz=AMS)) == MAX_ALL_DAY_SPAN


def test_an_unknown_time_zone_falls_back_rather_than_failing():
    odd = meeting()
    odd["start"]["timeZone"] = "Middle-earth Standard Time"
    assert parse_event(odd, tz=AMS)[0].day == date(2026, 9, 1)


def test_a_windows_zone_name_is_understood():
    """Graph names zones the Windows way, which is not the IANA way. 08:45 in
    W. Europe is 08:45 in Amsterdam, so the day must not move."""
    local = meeting(start="08:45:00", end="09:00:00")
    local["start"]["timeZone"] = "W. Europe Standard Time"
    assert parse_event(local, tz=AMS)[0].day == date(2026, 9, 1)


def test_an_event_with_no_start_is_an_error_not_an_empty_day():
    broken = meeting()
    del broken["start"]
    with pytest.raises(ValueError):
        parse_event(broken, tz=AMS)


def test_working_elsewhere_is_a_working_day_not_an_absence():
    (event,) = parse_event(meeting(showAs="workingElsewhere"), tz=AMS)
    assert event.show_as == "busy"
    assert not event.is_out_of_office


# ---------------------------------------------------------------------------
# reading a range
# ---------------------------------------------------------------------------


def test_a_desk_booking_survives_the_round_trip_to_the_classifier():
    """The whole point of the reader: what comes back has to satisfy the three
    signals is_desk_booking() insists on, using the shipped defaults."""
    events, degraded = read_range(
        date(2026, 9, 1),
        date(2026, 9, 4),
        search=fake_search([meeting(), all_day()]),
    )
    assert degraded is False
    result = classify_day(
        date(2026, 9, 3),
        [e for e in events if e.day == date(2026, 9, 3)],
        config=ClassifierConfig(),
    )
    assert result.verdict is Verdict.OFFICE
    assert Reason.BOOKING_PRESENT in result.reasons


def test_days_outside_the_requested_range_are_clipped():
    """The query reaches a month back to catch long absences; those extra days
    must not end up in the ledger."""
    events, degraded = read_range(
        date(2026, 9, 1),
        date(2026, 9, 4),
        search=fake_search([meeting(day="2026-08-10"), meeting(day="2026-09-02")]),
    )
    assert degraded is False
    assert [e.day for e in events] == [date(2026, 9, 2)]


def test_a_connector_failure_is_degraded_and_never_an_empty_week():
    def explode(_tool, **_kwargs):
        raise m365_mcp.M365Error("MCP HTTP 503")

    events, degraded = read_range(date(2026, 9, 1), date(2026, 9, 4), search=explode)
    assert (events, degraded) == ([], True)


def test_a_partial_answer_is_degraded():
    events, degraded = read_range(
        date(2026, 9, 1),
        date(2026, 9, 4),
        search=fake_search([meeting()], notes=["Results are partial: rate limited."]),
    )
    assert (events, degraded) == ([], True)


def test_a_truncated_answer_is_degraded():
    events, degraded = read_range(
        date(2026, 9, 1),
        date(2026, 9, 4),
        search=fake_search([meeting()], truncated=True),
    )
    assert (events, degraded) == ([], True)


def test_an_entirely_empty_window_is_a_question_not_a_week_of_home_days():
    """A wildcard search that quietly stopped matching returns a clean, empty,
    completely plausible answer. Believing it claims the home allowance for
    every day in the window."""
    events, degraded = read_range(date(2026, 9, 1), date(2026, 9, 4), search=fake_search([]))
    assert (events, degraded) == ([], True)

    result = classify_day(
        date(2026, 9, 1), [], config=ClassifierConfig(), calendar_degraded=degraded
    )
    assert result.verdict is Verdict.AMBIGUOUS
    assert Reason.CALENDAR_DEGRADED in result.reasons


def test_an_empty_window_can_be_accepted_when_asked_for_explicitly():
    events, degraded = read_range(
        date(2026, 9, 1), date(2026, 9, 4), require_events=False, search=fake_search([])
    )
    assert (events, degraded) == ([], False)


def test_every_event_failing_to_parse_is_degraded_not_empty():
    events, degraded = read_range(
        date(2026, 9, 1),
        date(2026, 9, 4),
        search=fake_search([{"id": "x", "subject": "moved format"}]),
    )
    assert (events, degraded) == ([], True)


def test_one_bad_event_among_good_ones_does_not_lose_the_week():
    events, degraded = read_range(
        date(2026, 9, 1),
        date(2026, 9, 4),
        search=fake_search([{"id": "x", "subject": "moved"}, meeting(day="2026-09-02")]),
    )
    assert degraded is False
    assert [e.day for e in events] == [date(2026, 9, 2)]


def test_the_query_reaches_back_far_enough_to_see_a_running_absence():
    seen: dict = {}

    def capture(_tool, **kwargs):
        seen.update(kwargs)
        return m365_mcp.SearchResult(items=[meeting()], notes=[], total=1, truncated=False)

    read_range(date(2026, 9, 1), date(2026, 9, 4), search=capture)
    assert seen["afterDateTime"] < "2026-08-05"
    assert seen["beforeDateTime"].startswith("2026-09-05")
    # Relevance ranking would return the 25 events the ranker liked, not the
    # events in the window.
    assert seen["order"] == "oldest"
