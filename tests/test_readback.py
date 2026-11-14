"""Reading what AFAS holds. The fixtures copy the portal's rendered text shape."""

from __future__ import annotations

from datetime import date

import pytest

from afas_declaraties.config import ConfigError
from afas_declaraties.insite import PortalChanged
from afas_declaraties.models import ClaimType
from afas_declaraties.readback import FiledLine, complete, parse_lines, position, read_lines

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
    def __init__(self, page, index, step):
        self.page, self.index, self.step = page, index, step

    def is_visible(self):
        return True

    def is_enabled(self):
        return 0 <= self.page.position[self.index] + self.step < len(self.page.grids[self.index])

    def click(self, timeout=None):
        self.page.stale = self.page.render()
        self.page.settling = self.page.lag
        self.page.position[self.index] += self.step


class StuckPager(FakePager):
    def is_enabled(self):
        return True

    def click(self, timeout=None):
        pass


class FakePagers:
    def __init__(self, page, step):
        self.page, self.step = page, step

    def count(self):
        return len(self.page.grids)

    def nth(self, index):
        return FakePager(self.page, index, self.step)


class FakePage:
    """Each grid is a list of pages; its Vorige and Volgende pagers walk it."""

    def __init__(self, *grids, lag=0, start=None):
        self.grids = grids
        self.position = list(start or [0] * len(grids))
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
        return FakePagers(self, -1 if name == "Vorige" else 1)


class StuckPage(FakePage):
    def get_by_role(self, role, name):
        pagers = super().get_by_role(role, name)
        if pagers.step == 1:
            pagers.nth = lambda index: StuckPager(self, index, 1)
        return pagers


class LatePagerPage(FakePage):
    """The rows are there before the pager is."""

    def __init__(self, *grids, pager_after):
        super().__init__(*grids)
        self.pager_after = pager_after

    def wait_for_timeout(self, ms):
        super().wait_for_timeout(ms)
        self.pager_after -= 1

    def get_by_role(self, role, name):
        pagers = super().get_by_role(role, name)
        if self.pager_after > 0:
            pagers.count = lambda: 0
        return pagers


class RestlessPage(FakePage):
    def inner_text(self, selector):
        self.visited.append("read")
        return super().inner_text(selector) + str(len(self.visited))


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


def test_a_grid_reopened_on_its_last_page_is_read_from_its_first(caplog):
    """InSite reopens the main grid where the session left it, so a second read
    started on its last page and saw only that page and the open grid."""
    open_grid = [
        row("maandag", "04-03-2030", "Medewerker", "Thuiswerkdagen"),
        row("dinsdag", "05-03-2030", "Medewerker", "Thuiswerkdagen"),
    ]
    main_grid = [
        row("maandag", "04-03-2030", "Medewerker", "Thuiswerkdagen"),
        row("dinsdag", "05-02-2030", "Akkoord", "Woon-werk"),
        row("maandag", "07-01-2030", "Akkoord", "Thuiswerkdagen"),
    ]
    filed = [date(2030, 2, 5), date(2030, 1, 7)]
    caplog.set_level("INFO")
    lines = read_lines(FakePage(open_grid, main_grid, start=[0, 2]), "host", "/claims")
    assert complete(lines, filed) == lines
    assert {line.day for line in lines} == {date(2030, 3, 4), date(2030, 3, 5), *filed}
    assert "after paging back 2" in caplog.text


def test_a_slow_pager_is_waited_for_and_not_read_as_the_end_of_the_list():
    """Reading on would leave a filed day looking absent, and it would be claimed twice."""
    pages = [
        row("maandag", "13-07-2026", "Akkoord", "Woon-werk"),
        row("maandag", "20-07-2026", "Akkoord", "Woon-werk"),
    ]
    lines = read_lines(FakePage(pages, lag=4), "host", "/claims")
    assert {line.day for line in lines} == {date(2026, 7, 13), date(2026, 7, 20)}


def test_a_pager_that_renders_after_the_rows_is_not_the_end_of_the_list(caplog):
    """Seen live, straight after a submit: the first page read as the whole list."""
    pages = [
        row("maandag", "13-07-2026", "Akkoord", "Woon-werk"),
        row("maandag", "20-07-2026", "Akkoord", "Woon-werk"),
    ]
    caplog.set_level("INFO")
    lines = read_lines(LatePagerPage(pages, pager_after=3), "host", "/claims")
    assert {line.day for line in lines} == {date(2026, 7, 13), date(2026, 7, 20)}
    assert "on 2 page(s)" in caplog.text


def test_a_page_that_never_stops_changing_is_a_failed_read():
    page = RestlessPage([row("maandag", "13-07-2026", "Akkoord", "Woon-werk")])
    with pytest.raises(PortalChanged, match="never stopped changing"):
        read_lines(page, "host", "/claims")


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


def test_a_read_without_a_day_known_to_be_filed_came_back_short():
    """A filed line stays on the list, so missing one means the read stopped early."""
    lines = [FiledLine(date(2026, 7, 13), "Akkoord", ClaimType.COMMUTE)]
    assert complete(lines, []) == lines
    assert complete(lines, [date(2026, 7, 13)]) == lines
    with pytest.raises(PortalChanged, match="came back short: 1 day"):
        complete(lines, [date(2026, 7, 13), date(2026, 7, 20)])
