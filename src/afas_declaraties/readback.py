"""What AFAS itself holds, read from the employee's own claim list.

The ledger records what this app meant to do. Only the portal can say what
happened, and lines also get filed and deleted by hand, so nothing is filed
without looking first and nothing counts as filed until it shows up here.

A failed read raises rather than returning nothing. "The page did not load" and
"nothing has been filed" must stay different answers, or the first one
double-claims. Same rule as COMMON_MISTAKES #8, pointing the other way.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from playwright.sync_api import Locator, Page

from .config import ConfigError
from .insite import PortalChanged
from .models import ClaimType

logger = logging.getLogger(__name__)

#: Still the employee's own draft: Aanmaken happened, Declaratie insturen did not.
STATUS_DRAFT = "Medewerker"
#: With the manager, or approved by them. Only these count as filed.
STATUS_FILED = frozenset({"Leidinggevende", "Akkoord"})

NEXT_PAGE = "Volgende"
PREVIOUS_PAGE = "Vorige"
# shortcut: 40 pages hold about 400 lines, some 18 months of claims. It raises when
# reached, and then the list needs a date filter rather than a bigger number.
MAX_PAGES = 40
PAGER_POLL_MS = 3_000
PAGER_POLLS = 10
#: Polls in a row without a change before a page with no clickable pager is the end.
END_POLLS = 3

_HEADER = "Datum boeking\tDatum\tStatus\tSoort declaratie"
_ROW = re.compile(r"(maandag|dinsdag|woensdag|donderdag|vrijdag|zaterdag|zondag)\t")
_DATE = re.compile(r"\d{2}-\d{2}-\d{4}")


@dataclass(frozen=True)
class FiledLine:
    """One declaration line as AFAS shows it."""

    day: date
    status: str
    claim_type: ClaimType | None  # None: a kind this app does not claim


@dataclass(frozen=True)
class Position:
    """Where a set of days stands in AFAS, for one claim type."""

    filed: list[date]
    draft: list[date]
    absent: list[date]
    #: The other claim type, a status not known here (a rejection, say), or more
    #: than one line on the day. Not safe to file over, and not proof of filing either.
    conflict: list[date]
    #: Open draft lines of this claim type on days outside the set.
    other_drafts: list[date]


def _claim_type(soort: str) -> ClaimType | None:
    soort = soort.lower()
    if soort.startswith("woon-werk"):
        return ClaimType.COMMUTE
    if soort.startswith("thuiswerk"):
        return ClaimType.HOME
    return None


def _grid_lines(grid: str) -> list[FiledLine]:
    """A row that cannot be read raises: skipping it would under-report what is filed."""
    lines = []
    for raw in grid.splitlines():
        row = raw.lstrip("\t ")
        if not _ROW.match(row):
            if "\t" in row and _DATE.search(row):
                raise PortalChanged(f"unreadable claim row: {row[:80]!r}")
            continue
        cells = [cell.strip() for cell in row.split("\t")]
        try:
            day = datetime.strptime(cells[1], "%d-%m-%Y").date()
            lines.append(FiledLine(day, cells[3], _claim_type(cells[4])))
        except (IndexError, ValueError) as exc:
            raise PortalChanged(f"unreadable claim row: {row[:80]!r}") from exc
    return lines


def parse_lines(text: str) -> list[FiledLine]:
    """Lines from one rendered page of the claim list.

    Columns: Dag, Datum boeking, Datum, Status, Soort declaratie, and more. An open
    draft shows in its own grid and again in the main one, so a line counts once per
    page unless one grid really holds it twice.
    """
    grids = text.replace("\n\t\n", "\t").split(_HEADER)
    if len(grids) < 2:
        raise PortalChanged("the claim list did not render the expected columns")

    counts: Counter[FiledLine] = Counter()
    for grid in grids[1:]:
        counts |= Counter(_grid_lines(grid))
    return list(counts.elements())


def _turn_page(page: Page, pager: Locator, text: str, name: str) -> str:
    """Click a pager and return the page it brought up.

    Only a disabled pager means the end. An enabled one that moves nothing is a slow
    or broken portal, and reading on would pass part of the list off as all of it,
    which makes a filed day look absent and gets it claimed twice.
    """
    pager.click(timeout=15_000)
    previous = text
    for _ in range(PAGER_POLLS):
        page.wait_for_timeout(PAGER_POLL_MS)
        current = page.inner_text("body")
        if current != text and current == previous:
            return current
        previous = current
    raise PortalChanged(f"a {name!r} pager is enabled but its page never settled")


def _live_pagers(page: Page, name: str) -> list[Locator]:
    pagers = page.get_by_role("button", name=name)
    return [
        pager
        for pager in (pagers.nth(i) for i in range(pagers.count()))
        if pager.is_visible() and pager.is_enabled()
    ]


def _settle(page: Page, text: str) -> str:
    """The page once END_POLLS reads in a row have not changed it."""
    quiet = 0
    for _ in range(PAGER_POLLS):
        page.wait_for_timeout(PAGER_POLL_MS)
        current = page.inner_text("body")
        quiet = quiet + 1 if current == text else 0
        text = current
        if quiet == END_POLLS:
            return text
    raise PortalChanged("the claim list never stopped changing")


def _rewind(page: Page, text: str) -> tuple[str, int]:
    """Every grid back on its first page, and how many pages that took.

    InSite reopens the main grid on the page the session last left it on, so every
    read after the first in a session starts on its last page.
    """
    for back in range(MAX_PAGES):
        live = _live_pagers(page, PREVIOUS_PAGE)
        if not live:
            text = _settle(page, text)
            live = _live_pagers(page, PREVIOUS_PAGE)
        if not live:
            return text, back
        text = _turn_page(page, live[0], text, PREVIOUS_PAGE)
    raise PortalChanged(f"the claim list was still paging back after {MAX_PAGES} pages")


def read_lines(page: Page, host: str, path: str) -> list[FiledLine]:
    """Every line on the claim list, across all its pagers, from each one's first page.

    A page can show its rows before its pager is ready. Taking that for the end
    passes the first page off as the whole list, so a page with nothing to click
    only ends the list once it has stopped changing.
    """
    if not path:
        raise ConfigError("CLAIMS_PAGE_PATH is required to read back what AFAS holds")

    page.goto(f"https://{host}{path}", wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(7_000)

    # shortcut: a day entered twice is missed if the page break falls between the two,
    # because the same line also comes round again on every page of the other grid.
    seen: Counter[FiledLine] = Counter()
    text, back = _rewind(page, page.inner_text("body"))
    for turned in range(MAX_PAGES):
        seen |= Counter(parse_lines(text))

        live = _live_pagers(page, NEXT_PAGE)
        if not live:
            text = _settle(page, text)
            seen |= Counter(parse_lines(text))
            live = _live_pagers(page, NEXT_PAGE)
        if not live:
            # This account has filed before, so an empty list is a failed read.
            if not seen:
                raise PortalChanged("the claim list rendered no lines at all")
            lines = list(seen.elements())
            kinds = Counter(
                f"{ln.claim_type.value if ln.claim_type else 'other'}/{ln.status}" for ln in lines
            )
            logger.info(
                "readback: %d line(s) in AFAS on %d page(s) after paging back %d %s",
                len(lines),
                turned + 1,
                back,
                dict(sorted(kinds.items())),
            )
            return lines
        text = _turn_page(page, live[0], text, NEXT_PAGE)

    raise PortalChanged(f"the claim list was still paging after {MAX_PAGES} pages")


def complete(lines: list[FiledLine], filed: Iterable[date]) -> list[FiledLine]:
    """``lines``, if they hold every day AFAS is known to hold as filed.

    A filed line stays on the list, so a read without one of them stopped short,
    and a decision taken from it would read a filed day it missed as absent.
    """
    missing = sorted(set(filed) - {line.day for line in lines})
    if missing:
        raise PortalChanged(
            f"the claim list came back short: {len(missing)} day(s) the ledger has as filed "
            f"are not on it ({missing[0]:%d-%m-%Y} to {missing[-1]:%d-%m-%Y}), so nothing "
            "was decided from it"
        )
    return lines


def position(lines: Iterable[FiledLine], claim_type: ClaimType, days: Iterable[date]) -> Position:
    """Sort ``days`` by what AFAS shows for them."""
    wanted = set(days)
    by_day: dict[date, list[FiledLine]] = {}
    for line in lines:
        if line.claim_type is not None:
            by_day.setdefault(line.day, []).append(line)

    filed, draft, absent, conflict = [], [], [], []
    for day in sorted(wanted):
        here = by_day.get(day, [])
        if not here:
            absent.append(day)
        elif len(here) > 1 or here[0].claim_type is not claim_type:
            conflict.append(day)
        elif here[0].status == STATUS_DRAFT:
            draft.append(day)
        elif here[0].status in STATUS_FILED:
            filed.append(day)
        else:
            conflict.append(day)

    other_drafts = sorted(
        day
        for day, here in by_day.items()
        if day not in wanted
        and any(line.claim_type is claim_type and line.status == STATUS_DRAFT for line in here)
    )
    return Position(filed, draft, absent, conflict, other_drafts)
