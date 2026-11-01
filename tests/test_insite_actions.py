"""Workflow-action selection.

The controls on a declaration task are banner links whose ids are
`P_R_W_<per-tenant-hex>_BannerItemLink_<n>`, and the index is ambiguous across
webparts: "_BannerItemLink_0" is BOTH "Declaratie insturen" and "Verwijderen".
Picking the wrong one deletes a draft instead of filing it, so the label is the
only safe discriminator and "exactly one match" is a hard requirement.
"""

from datetime import date

import pytest

from afas_declaraties.insite import (
    ACTION_DELETE,
    ACTION_SUBMIT,
    AmbiguousAction,
    ClaimLine,
    InSiteError,
    PortalChanged,
    available_actions,
    find_workflow_action,
    set_or_verify_quantity,
    set_reference,
    verify_row,
)


class FakeElement:
    def __init__(self, text, ident="", visible=True):
        self._text, self._id, self._visible = text, ident, visible
        self.clicked = False

    def is_visible(self):
        return self._visible

    def inner_text(self):
        return self._text

    def get_attribute(self, name):
        return self._id if name == "id" else None

    def click(self):
        self.clicked = True


class FakePage:
    def __init__(self, elements):
        self._elements = elements

    def query_selector_all(self, _selector):
        return self._elements


# Shapes taken from the live portal, hex fabricated. What is asserted is that
# two distinct webparts both end in _BannerItemLink_0, so the trailing index
# cannot tell submit from delete.
ACTIONS = [
    FakeElement("Declaratie insturen", "P_R_W_AAAA0000AAAA0000AAAA0000AAAA0001_BannerItemLink_0"),
    FakeElement("Verwijderen", "P_R_W_BBBB1111BBBB1111BBBB1111BBBB0002_BannerItemLink_0"),
    FakeElement("Aanpassen", "P_R_W_BBBB1111BBBB1111BBBB1111BBBB0002_BannerItemLink_1"),
]


def test_submit_and_delete_are_told_apart():
    page = FakePage(ACTIONS)
    assert (
        find_workflow_action(page, ACTION_SUBMIT).get_attribute("id").startswith("P_R_W_AAAA0000")
    )
    assert (
        find_workflow_action(page, ACTION_DELETE).get_attribute("id").startswith("P_R_W_BBBB1111")
    )


def test_a_missing_action_refuses_rather_than_falling_through():
    """Returning the first link here would delete the draft."""
    with pytest.raises(AmbiguousAction):
        find_workflow_action(FakePage(ACTIONS), "Declaratie insturen ")  # trailing space


def test_duplicate_labels_refuse():
    dupes = ACTIONS + [FakeElement("Declaratie insturen", "P_R_W_OTHER_BannerItemLink_0")]
    with pytest.raises(AmbiguousAction):
        find_workflow_action(FakePage(dupes), ACTION_SUBMIT)


def test_invisible_controls_are_ignored():
    hidden = [FakeElement("Declaratie insturen", "hidden", visible=False)] + ACTIONS
    assert find_workflow_action(FakePage(hidden), ACTION_SUBMIT).get_attribute("id") != "hidden"


def test_available_actions_lists_only_visible_labels():
    page = FakePage(ACTIONS + [FakeElement("Geheim", "x", visible=False)])
    assert available_actions(page) == ["Declaratie insturen", "Verwijderen", "Aanpassen"]


def test_verify_row_rejects_a_period_that_does_not_match_the_date():
    """The prefilled period is the CURRENT month, so an unset period silently
    books a past-month day into the wrong payroll period."""
    line = ClaimLine(date(2026, 8, 13))
    with pytest.raises(InSiteError, match="period"):
        verify_row(
            {"Datum boeking": "13-08-2026", "Jaar": "2026", "ref:Periode": "9", "Aantal": "1,00"},
            line,
        )


def test_verify_row_accepts_a_correct_row():
    line = ClaimLine(date(2026, 8, 13))
    verify_row(
        {"Datum boeking": "13-08-2026", "Jaar": "2026", "ref:Periode": "8", "Aantal": "1,00"}, line
    )


def test_verify_row_reads_1_and_1_00_as_the_same_count():
    """The woon-werk form holds 1,00 and the thuiswerk form holds 1."""
    line = ClaimLine(date(2026, 8, 13))
    verify_row(
        {"Datum boeking": "13-08-2026", "Jaar": "2026", "ref:Periode": "8", "Aantal": "1"}, line
    )


@pytest.mark.parametrize("wrong", ["2", "0,50", "", None, "een"])
def test_verify_row_still_refuses_a_different_count(wrong):
    line = ClaimLine(date(2026, 8, 13))
    with pytest.raises(InSiteError, match="quantity"):
        verify_row(
            {"Datum boeking": "13-08-2026", "Jaar": "2026", "ref:Periode": "8", "Aantal": wrong},
            line,
        )


class FakeQuantity:
    def __init__(self, value, disabled):
        self.value, self.disabled = value, disabled
        self.filled = []

    @property
    def first(self):
        return self

    def is_disabled(self, timeout=None):
        return self.disabled

    def input_value(self):
        return self.value

    def fill(self, value):
        self.filled.append(value)
        self.value = "1"

    def press(self, key):
        pass


class FakeFormPage:
    def __init__(self, field):
        self.field = field

    def locator(self, selector):
        return self.field

    def wait_for_timeout(self, ms):
        pass


@pytest.mark.parametrize("disabled", [True, False])
def test_a_quantity_that_already_holds_the_count_is_not_written(disabled):
    field = FakeQuantity("1", disabled)
    set_or_verify_quantity(FakeFormPage(field), 1, "1,00")
    assert field.filled == []


def test_a_fixed_quantity_that_differs_refuses():
    with pytest.raises(InSiteError, match="fixed the quantity"):
        set_or_verify_quantity(FakeFormPage(FakeQuantity("2,00", True)), 1, "1,00")


def test_an_editable_quantity_is_written_and_compared_as_a_number():
    """Written as 1,00 and read back as 1 must not burn three attempts and crash."""
    field = FakeQuantity("", False)
    set_or_verify_quantity(FakeFormPage(field), 1, "1,00")
    assert field.filled == ["1,00"]


class FakeMenuLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    @property
    def first(self):
        return self

    def _value(self):
        return self.selector.split("value='")[1].rstrip("']")

    def count(self):
        return int(self._value() in self.page.listed)

    def click(self, timeout=None):
        if "afas-menu-item" not in self.selector:
            return
        if self._value() not in self.page.listed:
            raise TimeoutError(self.selector)
        self.page.held = self._value()

    def get_attribute(self, name):
        return self.page.held


class FakeMenuPage:
    """Recent picks plus the first rows, until the search box is used."""

    def __init__(self, listed, searchable=True):
        self.listed, self.searchable = set(listed), searchable
        self.held, self.typed = None, []
        self.keyboard = self

    def locator(self, selector):
        return FakeMenuLocator(self, selector)

    def wait_for_timeout(self, ms):
        pass

    def type(self, text):
        self.typed.append(text)
        if self.searchable:
            self.listed.add(text)


MENU = ["8", "6", "7", "1", "2", "3"]


def test_a_period_the_menu_does_not_show_is_searched_for():
    """Why two monthly builds crashed: 9 is simply not in the first six rows."""
    page = FakeMenuPage(MENU)
    set_reference(page, 1, "PeId", "9")
    assert page.typed == ["9"]
    assert page.held == "9"


def test_a_period_the_menu_shows_is_picked_without_searching():
    page = FakeMenuPage(MENU)
    set_reference(page, 1, "PeId", "8")
    assert page.typed == []
    assert page.held == "8"


def test_a_period_search_cannot_find_refuses():
    page = FakeMenuPage(MENU, searchable=False)
    with pytest.raises(PortalChanged, match="PeId=9"):
        set_reference(page, 1, "PeId", "9")
    assert page.held is None
