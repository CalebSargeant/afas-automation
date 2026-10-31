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
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from playwright.sync_api import Page

from .config import ConfigError
from .insite import PortalChanged
from .models import ClaimType

logger = logging.getLogger(__name__)

#: Still the employee's own draft: Aanmaken happened, Declaratie insturen did not.
STATUS_DRAFT = "Medewerker"
#: With the manager, or approved by them. Only these count as filed.
STATUS_FILED = frozenset({"Leidinggevende", "Akkoord"})

NEXT_PAGE = "Volgende"
MAX_PAGES = 40

_HEADER = "Datum boeking\tDatum\tStatus\tSoort declaratie"
_ROW = re.compile(r"(maandag|dinsdag|woensdag|donderdag|vrijdag|zaterdag|zondag)\t")


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
    #: The other claim type, or a status not known here (a rejection, say). Not
    #: safe to file over, and not proof of filing either.
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


def parse_lines(text: str) -> list[FiledLine]:
    """Lines from one rendered page of the claim list.

    Columns: Dag, Datum boeking, Datum, Status, Soort declaratie, and more. A row
    that cannot be read raises: skipping it would under-report what is filed.
    """
    flat = text.replace("\n\t\n", "\t")
    if _HEADER not in flat:
        raise PortalChanged("the claim list did not render the expected columns")

    lines = []
    for raw in flat.splitlines():
        row = raw.lstrip("\t ")
        if not _ROW.match(row):
            continue
        cells = [cell.strip() for cell in row.split("\t")]
        try:
            day = datetime.strptime(cells[1], "%d-%m-%Y").date()
            lines.append(FiledLine(day, cells[3], _claim_type(cells[4])))
        except (IndexError, ValueError) as exc:
            raise PortalChanged(f"unreadable claim row: {row[:80]!r}") from exc
    return lines


def read_lines(page: Page, host: str, path: str) -> list[FiledLine]:
    """Every line on the claim list, across all its pagers."""
    if not path:
        raise ConfigError("CLAIMS_PAGE_PATH is required to read back what AFAS holds")

    page.goto(f"https://{host}{path}", wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(7_000)

    seen: dict[tuple, FiledLine] = {}
    spent: set[int] = set()
    text = page.inner_text("body")
    for _ in range(MAX_PAGES):
        for line in parse_lines(text):
            seen[(line.day, line.status, line.claim_type)] = line

        pagers = page.get_by_role("button", name=NEXT_PAGE)
        moved = False
        for i in range(pagers.count()):
            pager = pagers.nth(i)
            if i in spent or not (pager.is_visible() and pager.is_enabled()):
                continue
            pager.click(timeout=15_000)
            page.wait_for_timeout(4_500)
            after = page.inner_text("body")
            if after == text:
                spent.add(i)
                continue
            text, moved = after, True
            break

        if not moved:
            # This account has filed before, so an empty list is a failed read.
            if not seen:
                raise PortalChanged("the claim list rendered no lines at all")
            logger.info("readback: %d line(s) in AFAS", len(seen))
            return list(seen.values())

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
        statuses = {line.status for line in here}
        if not here:
            absent.append(day)
        elif any(line.claim_type is not claim_type for line in here):
            conflict.append(day)
        elif statuses == {STATUS_DRAFT}:
            draft.append(day)
        elif statuses <= STATUS_FILED:
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
