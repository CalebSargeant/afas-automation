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
        self.page.position[self.index] += 1


class FakePagers:
    def __init__(self, page):
        self.page = page

    def count(self):
        return len(self.page.grids)

    def nth(self, index):
        return FakePager(self.page, index)


class FakePage:
    """Each grid is a list of pages; each pager walks its own grid."""

    def __init__(self, *grids):
        self.grids = grids
        self.position = [0] * len(grids)
        self.visited = []

    def goto(self, url, **kw):
        self.visited.append(url)

    def wait_for_timeout(self, ms):
        pass

    def inner_text(self, selector):
        return grid(*[g[self.position[i]] for i, g in enumerate(self.grids)])

    def get_by_role(self, role, name):
        return FakePagers(self)


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


def test_a_list_with_no_lines_is_a_failed_read():
    page = FakePage([""])
    with pytest.raises(PortalChanged):
        read_lines(page, "host", "/claims")


def test_a_missing_page_path_is_a_config_error_not_an_empty_list():
    with pytest.raises(ConfigError):
        read_lines(FakePage([""]), "host", "")
