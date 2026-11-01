"""Which period `submit` files, and what counts as filed.

The submit job runs on a timer but must never work its period out from the
date. `build` works on periods well behind today, so a run that asks "what was
last month?" names a different month from the one the human approved. AFAS has
no undo, so the period comes from the approved row and nowhere else.

And a click is not a filing. Only the claim list in AFAS says it landed.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import pytest

from afas_declaraties import cli
from afas_declaraties.models import ClaimType
from afas_declaraties.readback import FiledLine

DAY = date(2026, 8, 27)


def submission(year, period, claim_type, digest="d0", state="approved"):
    return {
        "period_year": year,
        "period_no": period,
        "claim_type": claim_type.value,
        "lines_digest": digest,
        "state": state,
    }


class FakeStore:
    """Just enough of `store` for cmd_submit, recording what it was told."""

    def __init__(self, approved, days=None, digest="d0", unsettled=(), lock=True):
        self._approved = approved
        self._days = days if days is not None else [DAY]
        self._digest = digest
        self._unsettled = list(unsettled)
        self._lock = lock
        self.states: list[tuple] = []
        self.digests: list[str] = []
        self.marked: list[list[date]] = []
        self.claimable_calls: list[tuple] = []

    @contextmanager
    def connect(self, dsn):
        yield object()

    def ensure_schema(self, conn):
        pass

    def approved_submissions(self, conn):
        return list(self._approved)

    def unsettled_submissions(self, conn):
        return list(self._unsettled)

    def get_submission(self, conn, year, period, claim_type):
        for row in self._approved:
            if (row["period_year"], row["period_no"], row["claim_type"]) == (
                year,
                period,
                claim_type.value,
            ):
                return row
        return None

    def claimable_days(self, conn, year, period, claim_type):
        self.claimable_calls.append((year, period, claim_type))
        return [{"day": d} for d in self._days]

    def unresolved_days(self, conn, year, period):
        return []

    def days_in_period(self, conn, year, period):
        return [{"day": d} for d in self._days]

    def lines_digest(self, days, claim_type):
        return self._digest

    @contextmanager
    def browser_lock(self, conn):
        yield self._lock

    def set_submission_state(self, conn, year, period, claim_type, state, error=None, digest=None):
        self.states.append((year, period, claim_type, state, error))
        if digest:
            self.digests.append(digest)

    def mark_days_state(self, conn, days, state):
        self.marked.append(list(days))


@pytest.fixture
def cfg():
    return SimpleNamespace(
        database_url="postgresql://x/y",
        commute_page="forms/woon-werk",
        home_page="forms/thuiswerk",
        claims_page="/claims",
        insite_host="portal.example.invalid",
        profile_dir="/tmp/profile",
        op_item="item",
        op_vault="vault",
        dry_run=False,
        slack_bot_token="",
        slack_channel="",
        approver_ids=frozenset(),
    )


def args(**kw):
    return argparse.Namespace(year=kw.get("year"), period=kw.get("period"))


@pytest.fixture
def alerts(monkeypatch):
    said: list[str] = []
    monkeypatch.setattr(cli, "alert", lambda cfg, text: said.append(text))
    return said


@pytest.fixture
def no_browser(monkeypatch):
    """Fail loudly if a test reaches the portal; these are selection tests."""

    @contextmanager
    def boom(*a, **kw):
        raise AssertionError("cmd_submit opened a session when it should not have")

    monkeypatch.setattr(cli, "open_session", boom)


class Portal:
    """A claim list whose draft lines become filed lines when insturen is clicked."""

    def __init__(self, monkeypatch, lines, landed=True):
        self.lines = lines
        self.landed = landed
        self.clicks = 0

        @contextmanager
        def fake_session(host, **kw):
            yield (object(), object())

        monkeypatch.setattr(cli, "open_session", fake_session)
        monkeypatch.setattr(cli, "open_task_for", lambda *a, **kw: None)
        monkeypatch.setattr(cli, "submit_declaration", self._click)
        monkeypatch.setattr(cli, "read_lines", lambda *a, **kw: self.lines)
        monkeypatch.setattr(cli, "_post_card", lambda *a, **kw: None)

    def _click(self, page, dry_run):
        assert dry_run is False
        self.clicks += 1
        if self.landed:
            self.lines = [FiledLine(ln.day, "Leidinggevende", ln.claim_type) for ln in self.lines]
        return True


def draft(claim_type, days=(DAY,)):
    return [FiledLine(d, "Medewerker", claim_type) for d in days]


def states(store):
    return [(y, p, s) for y, p, _c, s, _e in store.states]


def test_year_without_period_is_refused(cfg, monkeypatch, no_browser):
    store = FakeStore([])
    monkeypatch.setattr(cli, "store", store)
    assert cli.cmd_submit(cfg, args(year=2026)) == 2


def test_period_without_year_is_refused(cfg, monkeypatch, no_browser):
    store = FakeStore([])
    monkeypatch.setattr(cli, "store", store)
    assert cli.cmd_submit(cfg, args(period=8)) == 2


def test_nothing_approved_is_a_clean_no_op(cfg, monkeypatch, no_browser):
    store = FakeStore([])
    monkeypatch.setattr(cli, "store", store)
    assert cli.cmd_submit(cfg, args()) == 0
    assert store.states == []


def test_bare_run_files_the_approved_period_not_the_current_one(cfg, monkeypatch, alerts):
    """The regression this exists for: August approved, submit run in September."""
    store = FakeStore([submission(2026, 8, ClaimType.COMMUTE)])
    monkeypatch.setattr(cli, "store", store)
    portal = Portal(monkeypatch, draft(ClaimType.COMMUTE))

    assert cli.cmd_submit(cfg, args()) == 0
    assert set(store.claimable_calls) == {(2026, 8, ClaimType.COMMUTE)}
    assert states(store) == [(2026, 8, "in_flight"), (2026, 8, "submitted")]
    assert portal.clicks == 1
    assert alerts == []


def test_each_approved_row_keeps_its_own_period(cfg, monkeypatch, alerts):
    store = FakeStore(
        [
            submission(2026, 7, ClaimType.HOME),
            submission(2026, 8, ClaimType.COMMUTE),
        ]
    )
    monkeypatch.setattr(cli, "store", store)
    Portal(monkeypatch, draft(ClaimType.HOME) + draft(ClaimType.COMMUTE, [date(2026, 8, 28)]))
    # Each claim type has one line in this portal, so give each its own day.
    store._days = [DAY]
    store.claimable_days = lambda conn, y, p, ct: (
        store.claimable_calls.append((y, p, ct))
        or [{"day": DAY if ct is ClaimType.HOME else date(2026, 8, 28)}]
    )

    assert cli.cmd_submit(cfg, args()) == 0
    assert list(dict.fromkeys(store.claimable_calls)) == [
        (2026, 7, ClaimType.HOME),
        (2026, 8, ClaimType.COMMUTE),
    ]


def test_a_digest_that_moved_since_approval_is_refused(cfg, monkeypatch, no_browser, alerts):
    store = FakeStore([submission(2026, 8, ClaimType.COMMUTE, digest="approved-digest")])
    store._digest = "something-else"
    monkeypatch.setattr(cli, "store", store)

    assert cli.cmd_submit(cfg, args()) == 1
    assert store.states == [
        (2026, 8, ClaimType.COMMUTE, "awaiting_approval", "digest changed after approval")
    ]
    assert "veranderd" in alerts[0]


def test_an_explicit_period_only_files_that_period(cfg, monkeypatch, alerts):
    store = FakeStore(
        [
            submission(2026, 7, ClaimType.HOME),
            submission(2026, 8, ClaimType.COMMUTE),
        ]
    )
    monkeypatch.setattr(cli, "store", store)
    Portal(monkeypatch, draft(ClaimType.HOME))

    assert cli.cmd_submit(cfg, args(year=2026, period=7)) == 0
    assert set(store.claimable_calls) == {(2026, 7, ClaimType.HOME)}


def test_a_dry_run_opens_no_browser_and_says_nothing_was_filed(cfg, monkeypatch, no_browser):
    cfg.dry_run = True
    store = FakeStore([submission(2026, 8, ClaimType.COMMUTE)])
    monkeypatch.setattr(cli, "store", store)
    said = []
    monkeypatch.setattr(cli, "notify", lambda cfg, text: said.append(text))

    assert cli.cmd_submit(cfg, args()) == 0
    assert states(store) == [(2026, 8, "drafted")]
    assert "niets ingestuurd" in said[0]


def test_a_click_afas_does_not_confirm_is_not_a_filing(cfg, monkeypatch, alerts):
    """The key test: `submitted` means AFAS shows it, and anything else reaches a human."""
    store = FakeStore([submission(2026, 8, ClaimType.COMMUTE)])
    monkeypatch.setattr(cli, "store", store)
    Portal(monkeypatch, draft(ClaimType.COMMUTE), landed=False)

    assert cli.cmd_submit(cfg, args()) == 1
    assert states(store) == [(2026, 8, "in_flight"), (2026, 8, "needs_reconciliation")]
    assert store.marked == []
    assert "bevestigt het niet" in alerts[0]


def test_a_crash_after_the_state_write_goes_to_reconciliation(cfg, monkeypatch, alerts):
    store = FakeStore([submission(2026, 8, ClaimType.COMMUTE)])
    monkeypatch.setattr(cli, "store", store)
    Portal(monkeypatch, draft(ClaimType.COMMUTE))

    def boom(*a, **kw):
        raise RuntimeError("the portal fell over")

    monkeypatch.setattr(cli, "open_task_for", boom)

    assert cli.cmd_submit(cfg, args()) == 1
    assert states(store) == [(2026, 8, "in_flight"), (2026, 8, "needs_reconciliation")]
    assert "fell over" in alerts[0]


def test_a_draft_that_does_not_match_is_never_clicked(cfg, monkeypatch, alerts):
    store = FakeStore([submission(2026, 8, ClaimType.COMMUTE)], days=[DAY, date(2026, 8, 31)])
    monkeypatch.setattr(cli, "store", store)
    portal = Portal(monkeypatch, draft(ClaimType.COMMUTE))

    assert cli.cmd_submit(cfg, args()) == 1
    assert portal.clicks == 0
    assert states(store) == [(2026, 8, "failed")], "left approved, build would skip it for good"
    assert "klopt niet" in alerts[0]


def test_another_open_draft_blocks_filing(cfg, monkeypatch, alerts):
    """The task list cannot tell two drafts apart, so the wrong month could be filed."""
    june = date(2026, 6, 2)
    store = FakeStore([submission(2026, 8, ClaimType.HOME)])
    monkeypatch.setattr(cli, "store", store)
    portal = Portal(monkeypatch, draft(ClaimType.HOME, [june, DAY]))

    assert cli.cmd_submit(cfg, args()) == 1
    assert portal.clicks == 0
    assert states(store) == [(2026, 8, "approved")], "the approved draft is intact, so it stands"
    assert "02-06-2026" in alerts[0]


def test_lines_already_filed_are_recorded_without_a_click(cfg, monkeypatch, alerts):
    store = FakeStore([submission(2026, 8, ClaimType.COMMUTE)])
    monkeypatch.setattr(cli, "store", store)
    portal = Portal(monkeypatch, [FiledLine(DAY, "Akkoord", ClaimType.COMMUTE)])

    assert cli.cmd_submit(cfg, args()) == 0
    assert portal.clicks == 0
    assert states(store) == [(2026, 8, "submitted")]


@pytest.mark.parametrize(
    "lines, expected_state, code",
    [
        ([FiledLine(DAY, "Leidinggevende", ClaimType.COMMUTE)], "submitted", 0),
        ([FiledLine(DAY, "Medewerker", ClaimType.COMMUTE)], "awaiting_approval", 1),
        ([FiledLine(date(2026, 6, 1), "Akkoord", ClaimType.COMMUTE)], "failed", 1),
        ([FiledLine(DAY, "Afgewezen", ClaimType.COMMUTE)], "failed", 1),
    ],
)
def test_an_interrupted_click_is_settled_from_afas_and_never_reclicked(
    cfg, monkeypatch, alerts, lines, expected_state, code
):
    row = submission(2026, 8, ClaimType.COMMUTE, state="in_flight")
    store = FakeStore([], unsettled=[row])
    monkeypatch.setattr(cli, "store", store)
    portal = Portal(monkeypatch, lines)

    assert cli.cmd_submit(cfg, args()) == code
    assert portal.clicks == 0
    assert states(store) == [(2026, 8, expected_state)]


def test_a_click_that_did_not_land_gets_its_card_before_its_state(cfg, monkeypatch, alerts):
    """Waiting on a card that never went out, nothing would ever post it again."""
    row = submission(2026, 8, ClaimType.COMMUTE, state="in_flight")
    store = FakeStore([], unsettled=[row])
    monkeypatch.setattr(cli, "store", store)
    Portal(monkeypatch, draft(ClaimType.COMMUTE))

    def slack_down(*a, **kw):
        raise RuntimeError("slack is down")

    monkeypatch.setattr(cli, "_post_card", slack_down)
    with pytest.raises(RuntimeError):
        cli.cmd_submit(cfg, args())
    assert store.states == [], "still unsettled, so the next run comes back to it"

    monkeypatch.setattr(cli, "_post_card", lambda *a, **kw: None)
    assert cli.cmd_submit(cfg, args()) == 1
    assert states(store) == [(2026, 8, "awaiting_approval")]
    assert store.digests == ["d0"], "the row has to match what the new card shows"


def test_a_held_lock_is_a_failed_run_and_alerts(cfg, monkeypatch, alerts):
    store = FakeStore([submission(2026, 8, ClaimType.COMMUTE)], lock=False)
    monkeypatch.setattr(cli, "store", store)

    assert cli.cmd_submit(cfg, args()) == 1
    assert "lock" in alerts[0]
    assert store.states == []
