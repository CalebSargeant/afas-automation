"""Reading which week is on screen.

Every navigation decision is made against this, so getting it wrong does not
fail loudly -- it reads a different week's events and classifies real office
days as working-from-home.
"""

import logging
from datetime import date

from afas_declaraties.calendar_owa import looks_like_event, read_week, rendered_week


class FakePage:
    """Stands in for a Playwright page; rendered_week only harvests labels."""

    def __init__(self, labels):
        self._labels = labels
        self.waits = 0

    def evaluate(self, _js):
        return self._labels

    def wait_for_timeout(self, _ms):
        # rendered_week retries: the header re-renders asynchronously after a
        # week change, so a single read can catch the page mid-update.
        self.waits += 1


# Verbatim label SHAPES from the live calendar. The navigation buttons describe
# the week they would move TO, so they carry a date range that is not the one on
# screen. Order matters: the nav button appears before the header in the DOM.
NAV_PREV = "Go to previous week \nAugust 24 – 28, 2026"
NAV_NEXT = "Go to next week \nSeptember 7 – 11, 2026"
HEADER = "August 31 – September 4, 2026, Jump to a specific date or date range."
EVENT = (
    "Booking (A / Desk 1.01 / Zone A), all day event, Monday, August 31, 2026, By deskbird, Free"
)


def test_header_wins_over_the_navigation_buttons():
    """The regression: matching the first range on the page read the PREVIOUS
    week, so the reader navigated from a baseline one week out and returned a
    different week's events under the requested week's name."""
    assert rendered_week(FakePage([NAV_PREV, NAV_NEXT, HEADER, EVENT])) == (
        date(2026, 8, 31),
        date(2026, 9, 4),
    )


def test_navigation_buttons_alone_are_not_treated_as_the_current_week():
    assert rendered_week(FakePage([NAV_PREV, NAV_NEXT])) is None


def test_same_month_range_parses():
    page = FakePage(["August 24 – 28, 2026, Jump to a specific date or date range."])
    assert rendered_week(page) == (date(2026, 8, 24), date(2026, 8, 28))


def test_returns_none_when_no_header_is_present():
    """Better to report "cannot tell" than to guess a week."""
    page = FakePage([EVENT])
    assert rendered_week(page) is None
    assert page.waits > 0, "should retry before giving up on the header"


def test_looks_like_event_distinguishes_events_from_chrome():
    assert looks_like_event(EVENT)
    for chrome in (NAV_PREV, NAV_NEXT, HEADER, "Go to today"):
        assert not looks_like_event(chrome), chrome


class CalendarPage(FakePage):
    """What read_week drives: the labels change when the page reloads."""

    def __init__(self, labels, after_reload=None):
        super().__init__(labels)
        self._after_reload = after_reload
        self.reloads = 0

    def goto(self, *_args, **_kwargs):
        pass

    def reload(self, *_args, **_kwargs):
        self.reloads += 1
        self._labels = self._after_reload


MONDAY = date(2026, 8, 31)


def test_events_without_a_header_get_one_reload():
    """The live failure: the grid rendered, the header did not, and one week
    marked a whole window degraded while the next week in the run read fine."""
    page = CalendarPage([EVENT], after_reload=[HEADER, EVENT])
    events, degraded = read_week(page, MONDAY)
    assert page.reloads == 1
    assert not degraded
    assert MONDAY in {e.day for e in events}


def test_a_header_still_missing_after_the_reload_is_degraded():
    page = CalendarPage([EVENT], after_reload=[EVENT])
    assert read_week(page, MONDAY) == ([], True)
    assert page.reloads == 1, "one reload, not a loop"


def test_losing_the_header_once_the_week_is_verified_does_not_crash():
    """The closing log line read the header again and unpacked the answer."""

    class HeaderLeaves(CalendarPage):
        reads = 0

        def evaluate(self, js):
            self.reads += 1
            # Two reads settle the first load, the third verifies the week.
            return super().evaluate(js) if self.reads <= 3 else [EVENT]

    events, degraded = read_week(HeaderLeaves([HEADER, EVENT]), MONDAY)
    assert not degraded
    assert [e.day for e in events] == [MONDAY]


class NotTheCalendar(CalendarPage):
    """A page with neither events nor a header, however often it is reloaded."""

    def __init__(self, labels, where):
        super().__init__(labels, after_reload=labels)
        self._where = where

    def evaluate(self, js):
        return self._where if "location.hostname" in js else super().evaluate(js)


def test_a_week_it_cannot_read_says_what_was_on_screen(caplog):
    """The live failure logged an empty header list and nothing else, so a
    sign-in pane, Outlook in another language and a changed Outlook all read
    the same. The page is named; its calendar content is not."""
    dutch = "Overleg, hele dag, maandag 31 augustus 2026, Door Iemand, Vrij"
    where = ["outlook.office.com", "/calendar/view/workweek", "Agenda - Iemand - Outlook", "nl-NL"]
    with caplog.at_level(logging.INFO):
        assert read_week(NotTheCalendar([dutch], where), MONDAY) == ([], True)
    for line in ("no events after first load", "reloading once", "refusing to guess"):
        record = next(r.getMessage() for r in caplog.records if line in r.getMessage())
        assert "page outlook.office.com/calendar/view/workweek" in record
        assert "title 'Agenda', lang 'nl-NL'" in record
        assert "1 labels, 0 like events, 0 with English dates" in record
    assert "Overleg" not in caplog.text
    assert "Iemand" not in caplog.text


SIGN_IN = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=x"


class SessionDidNotCarryOver(CalendarPage):
    """Outlook hands every load to the identity provider until someone signs in."""

    def __init__(self):
        super().__init__([], after_reload=[])
        self.url = SIGN_IN

    def goto(self, url, **_kwargs):
        if self.url != SIGN_IN:
            self.url, self._labels = url, [HEADER, EVENT]


def test_a_sign_in_on_the_way_to_outlook_is_answered():
    page = SessionDidNotCarryOver()
    answered = []

    def answer(p):
        answered.append(p)
        p.url = "https://outlook.cloud.microsoft/"

    events, degraded = read_week(page, MONDAY, answer_sign_in=answer)
    assert answered == [page]
    assert not degraded
    assert MONDAY in {e.day for e in events}
    assert page.reloads == 0


def test_without_a_way_to_sign_in_the_sign_in_page_is_degraded():
    assert read_week(SessionDidNotCarryOver(), MONDAY) == ([], True)
