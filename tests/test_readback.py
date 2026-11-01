"""Reading what AFAS holds. The fixtures copy the portal's rendered text shape."""

from __future__ import annotations

from datetime import date

import pytest

from afas_declaraties.config import ConfigError
from afas_declaraties.insite import PortalChanged
from afas_declaraties.models import ClaimType
from afas_declaraties.readback import FiledLine, parse_lines, position, read_lines

HEADER = "\n\t\n".join(
    [
        "Dag",
        "Datum boeking",
        "Datum",
        "Status",
        "Soort declaratie",
        "Omschrijving",
        "Aantal",
        "Totaalbedrag",
        "Bevat bijlage(n)",
        "Toelichting",
    ]
)


def row(weekday, booked, status, soort, amount="1,00"):
    return "\n\t\n".join(
        [weekday, booked, "\xa0", status, soort, "Omschrijving", amount, "\xa0", "", "\xa0"]
    )


def grid(*rows):
    return "Mijn declaraties\n" + HEADER + "\n\n\n\t\n\t\n\t\n" + "\n\n\n".join(rows) + "\n"


def test_lines_are_read_with_status_and_claim_type():
    text = grid(
        row("dinsdag", "02-06-2026", "Medewerker", "Thuiswerkdagen", "1"),
        row("maandag", "13-07-2026", "Akkoord", "Woon-werk: Aantal reisbewegingen"),
        row("woensdag", "15-07-2026", "Akkoord", "Zakelijke kilometers"),
    )
    assert parse_lines(text) == [
        FiledLine(date(2026, 6, 2), "Medewerker", ClaimType.HOME),
        FiledLine(date(2026, 7, 13), "Akkoord", ClaimType.COMMUTE),
        FiledLine(date(2026, 7, 15), "Akkoord", None),
    ]


def test_a_page_without_the_grid_is_a_failed_read_not_an_empty_one():
    """Treating this as "nothing filed" is how a portal change double-claims."""
    with pytest.raises(PortalChanged):
        parse_lines("Pagina niet gevonden")


def test_an_unreadable_row_raises_rather_than_under_reporting():
    with pytest.raises(PortalChanged):
        parse_lines(grid(row("dinsdag", "2 juni", "Medewerker", "Thuiswerkdagen")))


def test_a_row_that_lost_its_weekday_raises_rather_than_being_skipped():
    with pytest.raises(PortalChanged):
        parse_lines(grid(row("", "02-06-2026", "Medewerker", "Thuiswerkdagen")))


def test_an_open_draft_shown_in_both_grids_is_one_line():
    """The real page: the draft has a grid of its own and also sits in the main one."""
    line = row("dinsdag", "02-06-2026", "Medewerker", "Thuiswerkdagen")
    other = row("maandag", "01-06-2026", "Akkoord", "Woon-werk: Aantal reisbewegingen")
    assert parse_lines(grid(line) + grid(line, other)) == [
        FiledLine(date(2026, 6, 2), "Medewerker", ClaimType.HOME),
        FiledLine(date(2026, 6, 1), "Akkoord", ClaimType.COMMUTE),
    ]


def test_a_day_entered_twice_in_one_grid_stays_two_lines_and_is_a_conflict():
    """Filing that draft would claim the day twice while the ledger records it once."""
    day = date(2026, 9, 1)
    line = row("dinsdag", "01-09-2026", "Medewerker", "Thuiswerkdagen")
    lines = parse_lines(grid(line, line))
    assert len(lines) == 2
    assert position(lines, ClaimType.HOME, [day]).conflict == [day]


def test_position_sorts_days_by_what_afas_shows():
    lines = [
        FiledLine(date(2026, 9, 1), "Akkoord", ClaimType.COMMUTE),
        FiledLine(date(2026, 9, 2), "Leidinggevende", ClaimType.COMMUTE),
        FiledLine(date(2026, 9, 3), "Medewerker", ClaimType.COMMUTE),
        FiledLine(date(2026, 9, 4), "Akkoord", ClaimType.HOME),
        FiledLine(date(2026, 9, 7), "Afgewezen", ClaimType.COMMUTE),
        FiledLine(date(2026, 9, 9), "Akkoord", None),
        FiledLine(date(2026, 6, 2), "Medewerker", ClaimType.COMMUTE),
        FiledLine(date(2026, 6, 3), "Medewerker", ClaimType.HOME),
    ]
    days = [date(2026, 9, n) for n in (1, 2, 3, 4, 7, 8, 9)]
    pos = position(lines, ClaimType.COMMUTE, days)
    assert pos.filed == [date(2026, 9, 1), date(2026, 9, 2)]
    assert pos.draft == [date(2026, 9, 3)]
    assert pos.conflict == [date(2026, 9, 4), date(2026, 9, 7)]
    assert pos.absent == [date(2026, 9, 8), date(2026, 9, 9)], "other expense kinds do not block"
    assert pos.other_drafts == [date(2026, 6, 2)]


def test_a_day_both_drafted_and_filed_is_a_conflict():
    day = date(2026, 9, 1)
    lines = [
        FiledLine(day, "Akkoord", ClaimType.COMMUTE),
        FiledLine(day, "Medewerker", ClaimType.COMMUTE),
    ]
    assert position(lines, ClaimType.COMMUTE, [day]).conflict == [day]


class FakePager:
    def __init__(self, page, index):
        self.page, self.index = page, index

    def is_visible(self):
        return True

    def is_enabled(self):
        return self.page.position[self.index] < len(self.page.grids[self.index]) - 1

    def click(self, timeout=None):
        self.page.stale = self.page.render()
        self.page.settling = self.page.lag
        self.page.position[self.index] += 1


class StuckPager(FakePager):
    def is_enabled(self):
        return True

    def click(self, timeout=None):
        pass


class FakePagers:
    def __init__(self, page):
        self.page = page

    def count(self):
        return len(self.page.grids)

    def nth(self, index):
        return FakePager(self.page, index)


class FakePage:
    """Each grid is a list of pages; each pager walks its own grid."""

    def __init__(self, *grids, lag=0):
        self.grids = grids
        self.position = [0] * len(grids)
        self.visited = []
        self.lag = lag
        self.settling = 0
        self.stale = ""

    def goto(self, url, **kw):
        self.visited.append(url)

    def wait_for_timeout(self, ms):
        self.settling = max(0, self.settling - 1)

    def render(self):
        return grid(*[g[self.position[i]] for i, g in enumerate(self.grids)])

    def inner_text(self, selector):
        return self.stale if self.settling else self.render()

    def get_by_role(self, role, name):
        return FakePagers(self)


class StuckPage(FakePage):
    def get_by_role(self, role, name):
        pagers = FakePagers(self)
        pagers.nth = lambda index: StuckPager(self, index)
        return pagers


def test_every_page_of_every_pager_is_read():
    """A live draft of 21 lines pages its own grid, ahead of the main one."""
    open_grid = [
        row("dinsdag", "01-09-2026", "Medewerker", "Thuiswerkdagen"),
        row("woensdag", "02-09-2026", "Medewerker", "Thuiswerkdagen"),
    ]
    main_grid = [
        row("maandag", "13-07-2026", "Akkoord", "Woon-werk"),
        row("maandag", "20-07-2026", "Akkoord", "Woon-werk"),
        row("maandag", "27-07-2026", "Akkoord", "Woon-werk"),
    ]
    lines = read_lines(FakePage(open_grid, main_grid), "host", "/claims")
    assert {line.day for line in lines} == {
        date(2026, 9, 1),
        date(2026, 9, 2),
        date(2026, 7, 13),
        date(2026, 7, 20),
        date(2026, 7, 27),
    }


def test_a_slow_pager_is_waited_for_and_not_read_as_the_end_of_the_list():
    """Reading on would leave a filed day looking absent, and it would be claimed twice."""
    pages = [
        row("maandag", "13-07-2026", "Akkoord", "Woon-werk"),
        row("maandag", "20-07-2026", "Akkoord", "Woon-werk"),
    ]
    lines = read_lines(FakePage(pages, lag=4), "host", "/claims")
    assert {line.day for line in lines} == {date(2026, 7, 13), date(2026, 7, 20)}


def test_an_enabled_pager_that_never_moves_is_a_failed_read():
    page = StuckPage([row("maandag", "13-07-2026", "Akkoord", "Woon-werk")])
    with pytest.raises(PortalChanged):
        read_lines(page, "host", "/claims")


def test_a_list_with_no_lines_is_a_failed_read():
    page = FakePage([""])
    with pytest.raises(PortalChanged):
        read_lines(page, "host", "/claims")


def test_a_missing_page_path_is_a_config_error_not_an_empty_list():
    with pytest.raises(ConfigError):
        read_lines(FakePage([""]), "host", "")
