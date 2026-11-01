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
# shortcut: 40 pages hold about 400 lines, some 18 months of claims. It raises when
# reached, and then the list needs a date filter rather than a bigger number.
MAX_PAGES = 40
PAGER_POLL_MS = 3_000
PAGER_POLLS = 10

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


def _turn_page(page: Page, pager: Locator, text: str) -> str:
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
    raise PortalChanged(f"a {NEXT_PAGE!r} pager is enabled but its page never settled")


def read_lines(page: Page, host: str, path: str) -> list[FiledLine]:
    """Every line on the claim list, across all its pagers."""
    if not path:
        raise ConfigError("CLAIMS_PAGE_PATH is required to read back what AFAS holds")

    page.goto(f"https://{host}{path}", wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(7_000)

    # shortcut: a day entered twice is missed if the page break falls between the two,
    # because the same line also comes round again on every page of the other grid.
    seen: Counter[FiledLine] = Counter()
    text = page.inner_text("body")
    for _ in range(MAX_PAGES):
        seen |= Counter(parse_lines(text))

        pagers = page.get_by_role("button", name=NEXT_PAGE)
        live = [
            pager
            for pager in (pagers.nth(i) for i in range(pagers.count()))
            if pager.is_visible() and pager.is_enabled()
        ]
        if not live:
            # This account has filed before, so an empty list is a failed read.
            if not seen:
                raise PortalChanged("the claim list rendered no lines at all")
            lines = list(seen.elements())
            logger.info("readback: %d line(s) in AFAS", len(lines))
            return lines
        text = _turn_page(page, live[0], text)

    raise PortalChanged(f"the claim list was still paging after {MAX_PAGES} pages")


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
