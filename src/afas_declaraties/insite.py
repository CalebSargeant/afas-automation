"""Driving the AFAS InSite verzameldeclaratie forms.

Both claim types use the same form, differing only by page slug. One row per
day: booking date, year, period and a count.

Three things about this form drove the design, all established by driving the
real thing:

1. **Period does not follow the date.** A fresh row is prefilled with the
   *current* month regardless of the booking date, so filing August days in
   September silently books them into period 9. Every row therefore sets the
   period explicitly and verifies it.
2. **Every field lives in shadow DOM, and the value lives in a different place
   per component type.** Date and number components hold it on the inner
   ``<input>`` property; ``afas-reference`` holds it on the host's ``value``
   attribute. Reading the wrong one returns ``None`` and looks like an empty
   form.
3. **The window index is a per-page-load counter.** Fields are
   ``Window_<n>_Declaratie_HrZ50_<code>`` and ``<n>`` becomes 1 after the first
   ``Nieuw``, more if any dialog opened first. It is discovered, never assumed.

Nothing is persisted until ``Aanmaken``, and even that only creates a draft
task the employee can still edit or delete. The irreversible step is
"Declaratie insturen" on that task, which lives in :mod:`submit`, behind an
explicit human approval.
"""

from __future__ import annotations

import logging
import operator
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from playwright.sync_api import Page

logger = logging.getLogger(__name__)

TABLE = "Declaratie_HrZ50"
NEW_ROW_BUTTON = "Nieuw"

#: BOTH the row dialog and the page carry a button labelled "Aanmaken", and
#: they do very different things: Window_<n> confirms one row and closes its
#: dialog, Window_0 creates the whole declaration. Selecting either by label
#: means a row confirmation can file a declaration instead. Always address them
#: by id.
ROW_CONFIRM_ID = "Window_{window}_Actions_AntaUpdateCloseWebForm"
CREATE_DRAFT_ID = "Window_0_Actions_AntaUpdateCloseWebForm"

#: Reads every declaration field, using the right accessor for each component.
_READ_ROW_JS = """() => {
  const out = {}; const seen = new Set();
  const walk = (root, d) => {
    if (d > 25) return;
    for (const el of root.querySelectorAll('*')) {
      if (seen.has(el)) continue; seen.add(el);
      if (el.tagName === 'INPUT') {
        const a = el.getAttribute('aria-label');
        if (a) out[a] = el.value;
      }
      if (el.tagName.toLowerCase() === 'afas-reference') {
        const l = el.getAttribute('label');
        if (l) out['ref:' + l] = el.getAttribute('value');
      }
      if (el.shadowRoot) walk(el.shadowRoot, d + 1);
    }
  };
  walk(document, 0);
  return out;
}"""

#: Finds the window index actually in use, rather than assuming Window_1.
_WINDOW_INDEX_JS = """(table) => {
  const found = new Set(); const seen = new Set();
  const re = new RegExp('^Window_(\\\\d+)_' + table + '_');
  const walk = (root, d) => {
    if (d > 25) return;
    for (const el of root.querySelectorAll('*')) {
      if (seen.has(el)) continue; seen.add(el);
      const m = (el.id || '').match(re);
      if (m) found.add(parseInt(m[1], 10));
      if (el.shadowRoot) walk(el.shadowRoot, d + 1);
    }
  };
  walk(document, 0);
  return [...found].sort((a, b) => b - a);
}"""


class InSiteError(RuntimeError):
    """The portal did not behave as the driver expects."""


class PortalChanged(InSiteError):
    """A control or field could not be found at all.

    Distinct from an ordinary failure because it means the portal moved, and
    the correct response is to stop and alert rather than retry.
    """


@dataclass(frozen=True)
class ClaimLine:
    """One day on a collective declaration."""

    day: date
    quantity: str = "1,00"

    @property
    def dutch_date(self) -> str:
        return self.day.strftime("%d-%m-%Y")

    @property
    def period(self) -> int:
        """AFAS period for this line. Calendar months in this environment,
        confirmed from the period lookup (Januari=1 … Augustus=8)."""
        return self.day.month


def _window_index(page: Page) -> int:
    indices = page.evaluate(_WINDOW_INDEX_JS, TABLE)
    if not indices:
        raise PortalChanged(f"no {TABLE} fields on the page; the form did not open")
    return indices[0]


def read_row(page: Page) -> dict[str, str | None]:
    """Current values of the visible declaration row."""
    return page.evaluate(_READ_ROW_JS)


def add_row(page: Page, *, timeout_ms: int = 20_000) -> int:
    """Click Nieuw and return the window index of the row dialog it opened.

    Each row is entered in a modal dialog. While one is open a screenblocker
    overlay covers the page, so Nieuw cannot be clicked again until the current
    row is confirmed -- which is what :func:`confirm_row` is for.
    """
    try:
        page.get_by_role("button", name=NEW_ROW_BUTTON).first.click(timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001
        raise PortalChanged(
            f"could not click {NEW_ROW_BUTTON!r}; a row dialog may still be open"
        ) from exc
    page.wait_for_timeout(3_000)
    index = _window_index(page)
    if index == 0:
        raise PortalChanged("Nieuw did not open a row dialog")
    logger.debug("insite: new row dialog opened as Window_%d", index)
    return index


def confirm_row(page: Page, window: int, *, timeout_ms: int = 20_000) -> None:
    """Commit the open row dialog to the grid.

    Addressed by id, never by label: the page-level Aanmaken carries the same
    text and creates the declaration itself.
    """
    button = "#" + ROW_CONFIRM_ID.format(window=window)
    try:
        page.locator(button).first.click(timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001
        raise PortalChanged(f"could not confirm row via {button}") from exc
    page.wait_for_timeout(3_500)
    logger.debug("insite: row Window_%d committed", window)


def same_quantity(a: str | None, b: str | None) -> bool:
    """One form shows the count as ``1,00`` and another as ``1``. Same count."""
    try:
        return float((a or "").replace(",", ".")) == float((b or "").replace(",", "."))
    except ValueError:
        return False


def set_text_field(
    page: Page,
    window: int,
    code: str,
    value: str,
    *,
    attempts: int = 3,
    same: Callable[[str, str], bool] = operator.eq,
) -> None:
    """Set a date/number field and confirm the value stuck.

    InSite performs async server round-trips that silently discard a value
    typed a moment earlier. Writing without reading back is how a row ends up
    filed with yesterday's date and no error anywhere.
    """
    selector = f"#Window_{window}_{TABLE}_{code} input"
    for attempt in range(attempts):
        try:
            field = page.locator(selector).first
            field.fill(value)
            field.press("Tab")
        except Exception as exc:  # noqa: BLE001
            raise PortalChanged(f"field {code} not fillable at {selector}") from exc
        page.wait_for_timeout(1_500)
        if same(page.locator(selector).first.input_value(), value):
            return
        logger.warning("insite: %s did not retain %r (attempt %d)", code, value, attempt + 1)
    raise InSiteError(f"{code} would not hold the value {value!r} after {attempts} attempts")


def set_reference(page: Page, window: int, code: str, value: str, *, attempts: int = 3) -> None:
    """Set an ``afas-reference`` lookup by picking its menu item.

    The inner combobox cannot be clicked -- an overlay span legitimately
    intercepts pointer events -- so the host component is the click target and
    the choice is made from the menu.

    The menu shows recent picks and the first few rows, not every value. Opening
    it focuses its search box, so a value it does not show is typed there.
    """
    host = f"#Window_{window}_{TABLE}_{code}_typeahead-base"
    item = page.locator(f"afas-menu-item[value='{value}']")
    for attempt in range(attempts):
        try:
            page.locator(host).first.click(timeout=10_000)
            page.wait_for_timeout(1_500)
            if not item.count():
                page.keyboard.type(value)
                page.wait_for_timeout(1_500)
            item.first.click(timeout=10_000)
        except Exception as exc:  # noqa: BLE001
            if attempt == attempts - 1:
                raise PortalChanged(f"could not select {code}={value} via {host}") from exc
            continue
        page.wait_for_timeout(2_500)
        if page.locator(host).first.get_attribute("value") == value:
            return
        logger.warning("insite: %s did not retain %r (attempt %d)", code, value, attempt + 1)
    raise InSiteError(f"{code} would not hold the value {value!r}")


def set_or_verify_quantity(page: Page, window: int, expected: str) -> None:
    """Set the count, or assert it if AFAS owns it.

    On the woon-werk form ``Aantal`` renders disabled: one row is one day's
    travel, both ways, and the value is fixed at 1,00 by the declaration
    profile. Writing to it is impossible and trying is a hard error. Other
    profiles may leave it editable, so adapt rather than assume, and either way
    confirm the number that will actually be filed.
    """
    selector = f"#Window_{window}_{TABLE}_Qu input"
    field = page.locator(selector).first
    try:
        disabled = field.is_disabled(timeout=10_000)
    except Exception as exc:  # noqa: BLE001
        raise PortalChanged(f"quantity field missing at {selector}") from exc

    actual = field.input_value()
    if same_quantity(actual, expected):
        logger.debug("insite: quantity is already %s", actual)
        return
    if disabled:
        raise InSiteError(
            f"AFAS fixed the quantity at {actual!r} but {expected!r} was intended. "
            "Filing this would claim a different amount than the ledger recorded."
        )
    set_text_field(page, window, "Qu", expected, same=same_quantity)


def fill_row(page: Page, window: int, line: ClaimLine) -> dict[str, str | None]:
    """Fill one claim line and return the verified row contents."""
    set_text_field(page, window, "DaTi", line.dutch_date)
    set_or_verify_quantity(page, window, line.quantity)
    # After the date, because the period must override the prefilled default
    # and a later date edit could reset it.
    set_reference(page, window, "PeId", str(line.period))

    row = read_row(page)
    verify_row(row, line)
    logger.info(
        "insite: row ready date=%s period=%s qty=%s", line.dutch_date, line.period, line.quantity
    )
    return row


def verify_row(row: dict[str, str | None], line: ClaimLine) -> None:
    """Assert the form holds exactly what was intended.

    The period check is the one that matters most: it is the difference
    between a claim landing in the right payroll month and silently landing in
    the wrong one, with no error shown anywhere.
    """
    actual_date = row.get("Datum boeking")
    if actual_date != line.dutch_date:
        raise InSiteError(f"date mismatch: form has {actual_date!r}, expected {line.dutch_date!r}")

    actual_period = row.get("ref:Periode")
    if actual_period != str(line.period):
        raise InSiteError(
            f"period mismatch for {line.day}: form has {actual_period!r}, expected "
            f"{line.period!r}. Filing this would book the day into the wrong payroll month."
        )

    actual_year = row.get("Jaar")
    if actual_year != str(line.day.year):
        raise InSiteError(f"year mismatch: form has {actual_year!r}, expected {line.day.year}")

    actual_qty = row.get("Aantal")
    if not same_quantity(actual_qty, line.quantity):
        raise InSiteError(f"quantity mismatch: form has {actual_qty!r}, expected {line.quantity!r}")


def create_draft(page: Page, *, dry_run: bool = True) -> bool:
    """Click Aanmaken, creating a reversible draft task.

    This is not the submission. It produces a "Declaratie voorbereiden" task in
    the employee's own list, which can still be amended (Aanpassen) or deleted
    (Verwijderen). Returns True if the click happened.
    """
    if dry_run:
        logger.info("insite: DRY RUN -- not creating the declaration")
        return False
    button = "#" + CREATE_DRAFT_ID
    try:
        page.locator(button).first.click(timeout=20_000)
    except Exception as exc:  # noqa: BLE001
        raise PortalChanged(f"could not create the declaration via {button}") from exc
    page.wait_for_timeout(5_000)
    logger.info("insite: draft declaration created")
    return True


# ---------------------------------------------------------------------------
# Workflow actions on a "Declaratie voorbereiden" task.
#
# These render as banner links, `a.webBanner-banneritem`, whose ids are
# `P_R_W_<32-hex>_BannerItemLink_<n>`. Selecting one by id is NOT safe here,
# for two reasons that compound:
#
#   Declaratie insturen  ->  P_R_W_<webpart A>_BannerItemLink_0
#   Verwijderen          ->  P_R_W_<webpart B>_BannerItemLink_0
#   Aanpassen            ->  P_R_W_<webpart B>_BannerItemLink_1
#
# The hex is a per-tenant webpart id, so it is not portable, and the trailing
# index is ambiguous across webparts -- "_BannerItemLink_0" is both "file the
# declaration" and "delete the draft". Matching on the visible label is the
# safer discriminator, which is the exact inverse of the Aanmaken buttons above
# where the labels collide and the ids are unique. Neither rule generalises;
# each control needs whichever attribute is actually unique.
# ---------------------------------------------------------------------------

BANNER_LINK = "a.webBanner-banneritem"
ACTION_SUBMIT = "Declaratie insturen"
ACTION_AMEND = "Aanpassen"
ACTION_DELETE = "Verwijderen"

TASK_LABEL = "Declaratie voorbereiden"


class AmbiguousAction(InSiteError):
    """More than one control matched, or none did.

    Never resolved by picking the first: the neighbouring control deletes the
    draft.
    """


def find_workflow_action(page: Page, label: str):
    """Return the single banner link whose label is exactly ``label``."""
    matches = []
    for link in page.query_selector_all(BANNER_LINK):
        try:
            if not link.is_visible():
                continue
            if (link.inner_text() or "").strip() == label:
                matches.append(link)
        except Exception:  # noqa: BLE001 - detached mid-scan
            continue
    if len(matches) != 1:
        raise AmbiguousAction(
            f"expected exactly one {label!r} action, found {len(matches)}. "
            "Refusing to guess: the adjacent control deletes the draft."
        )
    return matches[0]


def available_actions(page: Page) -> list[str]:
    """Labels of every workflow action on the current task, for logging."""
    out = []
    for link in page.query_selector_all(BANNER_LINK):
        try:
            if link.is_visible():
                text = (link.inner_text() or "").strip()
                if text:
                    out.append(text)
        except Exception:  # noqa: BLE001
            continue
    return out


def open_task_for(page: Page, home_url: str, slug: str, *, timeout_ms: int = 90_000) -> None:
    """Open the single "Declaratie voorbereiden" task whose form matches ``slug``.

    Task pages are not directly addressable -- navigating to their ``?SbId=``
    URL returns "pagina niet gevonden" -- so each candidate has to be opened
    from the start page and identified by where it lands.

    The task list gives no period, only "Verzameldeclaratie <name>", so two
    open drafts of the same claim type are indistinguishable from here. That is
    refused rather than guessed: filing the wrong month is not recoverable.
    """

    # The label also matches hidden nodes (templates, collapsed panels), so the
    # list must be filtered to what a user could actually click -- otherwise
    # nth() lands on an invisible element and the click times out.
    def open_nth(i: int) -> bool:
        page.goto(home_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(7_000)
        tasks = page.get_by_text(TASK_LABEL, exact=False).filter(visible=True)
        if i >= tasks.count():
            return False
        tasks.nth(i).click()
        page.wait_for_timeout(8_000)
        return True

    matched = 0
    index = 0
    while open_nth(index):
        if slug in page.url:
            matched += 1
        index += 1

    if matched == 0:
        raise AmbiguousAction(f"no {TASK_LABEL!r} task leads to a {slug!r} declaration")
    if matched > 1:
        raise AmbiguousAction(
            f"{matched} open {slug!r} drafts and no way to tell them apart from the "
            "task list. Refusing to guess which period to file."
        )

    for i in range(index):
        if open_nth(i) and slug in page.url:
            return
    raise AmbiguousAction(f"the {slug!r} task disappeared while re-opening it")


def submit_declaration(page: Page, *, dry_run: bool = True) -> bool:
    """File the open declaration with the manager. IRREVERSIBLE when live.

    There is deliberately no line-count assertion here: the task page shows
    workflow metadata and the action links, not the claim lines, so any such
    check would either always fail or be checking a coincidence. Content is
    verified where the content actually is: the caller compares the claim list
    in :mod:`readback` before and after this. True means the click happened, and
    only that list says whether it landed.
    """
    action = find_workflow_action(page, ACTION_SUBMIT)

    if dry_run:
        logger.info(
            "insite: DRY RUN -- %r located and not clicked (actions present: %s)",
            ACTION_SUBMIT,
            ", ".join(available_actions(page)),
        )
        return False

    action.click()
    page.wait_for_timeout(6_000)
    logger.info("insite: declaration submitted")
    return True
