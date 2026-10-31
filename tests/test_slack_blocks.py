"""Block Kit payloads and the approval encoding."""

from datetime import date

import pytest

from afas_declaraties.models import ClaimType
from afas_declaraties.slack_blocks import (
    APPROVE,
    SEP,
    approval_card,
    corrections_modal,
    decision_value,
    parse_decision_value,
    weekly_digest,
)

DAYS = [date(2026, 8, 13), date(2026, 8, 31)]


def test_decision_value_round_trips():
    v = decision_value(2026, 8, ClaimType.COMMUTE, "abc123")
    assert parse_decision_value(v) == (2026, 8, ClaimType.COMMUTE, "abc123")


@pytest.mark.parametrize("bad", ["2026|8|commute", "2026|8|commute|d|extra", "", "garbage"])
def test_malformed_decision_values_are_refused(bad):
    """A value with the wrong arity is a bug or a forged click, never a decision."""
    with pytest.raises(ValueError):
        parse_decision_value(bad)


def test_approve_button_carries_the_digest():
    blocks = approval_card(
        year=2026,
        period=8,
        claim_type=ClaimType.COMMUTE,
        days=DAYS,
        digest="deadbeef",
        unresolved=[],
        dry_run=False,
    )
    actions = next(b for b in blocks if b["type"] == "actions")
    approve = next(e for e in actions["elements"] if e["action_id"] == APPROVE)
    assert approve["value"].endswith("deadbeef")
    assert "confirm" in approve, "an irreversible action needs a confirm dialog"


def test_dropped_days_are_named_on_the_approval_card():
    """A day omitted for lack of an answer must be visible at approval time."""
    blocks = approval_card(
        year=2026,
        period=8,
        claim_type=ClaimType.COMMUTE,
        days=DAYS,
        digest="d",
        unresolved=[date(2026, 8, 20)],
        dry_run=True,
    )
    text = " ".join(str(b) for b in blocks)
    assert "20-08" in text
    assert "Niet meegenomen" in text


def test_the_card_names_days_left_out_after_reading_afas():
    """Approving "2 days" means little if the reader cannot see what was skipped."""
    blocks = approval_card(
        year=2026,
        period=8,
        claim_type=ClaimType.HOME,
        days=DAYS,
        digest="d",
        unresolved=[],
        dry_run=False,
        already=[date(2026, 8, 3)],
        unrecorded=[date(2026, 8, 4)],
        open_drafts=[date(2026, 6, 2)],
    )
    text = " ".join(str(b) for b in blocks)
    assert "Al in AFAS* (1): 03-08" in text
    assert "Nooit geclassificeerd* (1): 04-08" in text
    assert "Open concept in AFAS* (1): 02-06" in text


def test_a_clean_card_has_no_warning_sections():
    blocks = approval_card(
        year=2026,
        period=8,
        claim_type=ClaimType.HOME,
        days=DAYS,
        digest="d",
        unresolved=[],
        dry_run=False,
    )
    text = " ".join(str(b) for b in blocks)
    assert "Al in AFAS" not in text
    assert "Nooit geclassificeerd" not in text
    assert "Open concept" not in text


def test_dry_run_is_stated_on_the_card():
    blocks = approval_card(
        year=2026,
        period=8,
        claim_type=ClaimType.HOME,
        days=DAYS,
        digest="d",
        unresolved=[],
        dry_run=True,
    )
    assert "DRY RUN" in " ".join(str(b) for b in blocks)


def test_corrections_modal_block_ids_parse_back_to_dates():
    rows = [{"day": d, "claim_type": "commute", "state": "planned", "reasons": []} for d in DAYS]
    modal = corrections_modal(rows, private_metadata="period|2026|8")
    ids = [b["block_id"] for b in modal["blocks"]]
    assert [date.fromisoformat(i.split(SEP, 1)[1]) for i in ids] == DAYS


def test_untouched_days_are_optional_inputs():
    """Every input is optional so leaving a day alone means 'no change'."""
    rows = [{"day": DAYS[0], "claim_type": None, "state": "needs_input", "reasons": ["x"]}]
    modal = corrections_modal(rows, private_metadata="week|2026-08-31")
    assert modal["blocks"][0]["optional"] is True


def test_weekly_digest_leads_with_unresolved_days():
    rows = [
        {"day": DAYS[0], "claim_type": "commute", "state": "planned", "reasons": []},
        {
            "day": DAYS[1],
            "claim_type": None,
            "state": "needs_input",
            "reasons": ["booking_withdrawn"],
        },
    ]
    text = " ".join(str(b) for b in weekly_digest(rows, week_start=date(2026, 8, 31)))
    assert "Onbeslist" in text and "booking_withdrawn" in text
