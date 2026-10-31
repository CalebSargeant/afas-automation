"""What `build` does each day, with the ledger and the portal faked.

The rows of one month used to be one shot on the 28th: a crash lost the month and
nothing said so. The rules here are what replaced that.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import pytest

from afas_declaraties import cli
from afas_declaraties.models import ClaimType, DayState
from afas_declaraties.readback import FiledLine

SEP_1, SEP_2 = date(2026, 9, 1), date(2026, 9, 2)
JUNE = date(2026, 6, 2)


@pytest.mark.parametrize(
    "today, due",
    [
        (date(2026, 10, 27), (2026, 8)),
        (date(2026, 10, 28), (2026, 9)),
        (date(2026, 11, 1), (2026, 9)),
        (date(2027, 1, 5), (2026, 11)),
        (date(2027, 1, 28), (2026, 12)),
    ],
)
def test_a_month_falls_due_on_the_28th_of_the_month_after_it(today, due):
    assert cli._latest_due(today) == due


class FakeStore:
    def __init__(self, days=None, submissions=None, unresolved=(), periods=None, lock=True):
        self._days = days or {}
        self._submissions = submissions or {}
        self._unresolved = list(unresolved)
        self._periods = periods if periods is not None else [(2026, 9)]
        self._lock = lock
        self.marked: list[tuple] = []
        self.upserts: list[tuple] = []
        self.states: list[tuple] = []
        self.said: set[str] = set()

    @contextmanager
    def connect(self, dsn):
        yield object()

    def ensure_schema(self, conn):
        pass

    def open_periods(self, conn, year, period):
        return self._periods

    def claimable_days(self, conn, year, period, claim_type):
        return [{"day": d} for d in self._days.get((year, period, claim_type), [])]

    def days_in_period(self, conn, year, period):
        return [{"day": d} for days in self._days.values() for d in days] or [{"day": SEP_1}]

    def unresolved_days(self, conn, year, period):
        return [{"day": d} for d in self._unresolved]

    def get_submission(self, conn, year, period, claim_type):
        return self._submissions.get((year, period, claim_type))

    def lines_digest(self, days, claim_type):
        return claim_type.value + ",".join(d.isoformat() for d in days)

    def period_for(self, day):
        return day.year, day.month

    def mark_days_state(self, conn, days, state):
        self.marked.append((list(days), state))

    def upsert_submission(self, conn, y, p, ct, digest, n, state, *, error=None):
        self.upserts.append((y, p, ct, n, state))

    def set_submission_state(self, conn, y, p, ct, state, *, error=None, insite_ref=None):
        self.states.append((y, p, ct, state))

    def interaction_seen(self, conn, key):
        return key in self.said

    def claim_slack_interaction(self, conn, key, kind, actor):
        fresh = key not in self.said
        self.said.add(key)
        return fresh

    @contextmanager
    def browser_lock(self, conn):
        yield self._lock


@pytest.fixture
def cfg():
    return SimpleNamespace(
        database_url="postgresql://x/y",
        commute_page="/forms/woon-werk",
        home_page="/forms/thuiswerk",
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


class Portal:
    """A form and a claim list. Aanmaken adds the entered rows as draft lines."""

    def __init__(self, monkeypatch, lines=(), saves=True):
        self.lines = list(lines)
        self.saves = saves
        self.rows: list[date] = []
        self.created = 0
        self.rehearsed = 0
        self.cards: list[dict] = []
        self.alerts: list[str] = []
        self.sessions = 0

        @contextmanager
        def fake_session(host, **kw):
            self.sessions += 1
            yield (object(), object())

        monkeypatch.setattr(cli, "open_session", fake_session)
        monkeypatch.setattr(cli, "read_lines", lambda *a, **kw: list(self.lines))
        monkeypatch.setattr(cli, "_fill_rows", self._fill)
        monkeypatch.setattr(cli, "create_draft", self._create)
        monkeypatch.setattr(cli, "alert", lambda cfg, text: self.alerts.append(text) or True)
        monkeypatch.setattr(
            cli,
            "_post_card",
            lambda cfg, conn, y, p, ct, days, **kw: self.cards.append(
                {"period": (y, p), "type": ct, "days": days, **kw}
            ),
        )

    def _fill(self, cfg, page, path, days):
        self.rows = list(days)
        self.path = path

    def _create(self, page, *, dry_run):
        if dry_run:
            self.rehearsed += 1
            return False
        self.created += 1
        if self.saves:
            self.lines += [FiledLine(d, "Medewerker", self.claim_type) for d in self.rows]
        return True

    claim_type = ClaimType.COMMUTE


def build(cfg, period="2026-09"):
    return cli.cmd_build(cfg, argparse.Namespace(period=period))


def commute(*days):
    return {(2026, 9, ClaimType.COMMUTE): list(days)}


def line(day, status, claim_type=ClaimType.COMMUTE):
    return FiledLine(day, status, claim_type)


def test_live_run_creates_the_draft_and_confirms_it_in_afas(cfg, monkeypatch):
    store = FakeStore(commute(SEP_1, SEP_2))
    monkeypatch.setattr(cli, "store", store)
    portal = Portal(monkeypatch)

    assert build(cfg) == 0
    assert portal.rows == [SEP_1, SEP_2]
    assert portal.created == 1
    assert store.marked == [([SEP_1, SEP_2], DayState.DRAFTED)]
    assert store.upserts == [(2026, 9, ClaimType.COMMUTE, 2, "awaiting_approval")]
    assert portal.cards[0]["days"] == [SEP_1, SEP_2]
    assert portal.alerts == []


def test_a_draft_afas_does_not_show_fails_and_alerts(cfg, monkeypatch):
    store = FakeStore(commute(SEP_1))
    monkeypatch.setattr(cli, "store", store)
    portal = Portal(monkeypatch, saves=False)

    assert build(cfg) == 1
    assert store.upserts == [(2026, 9, ClaimType.COMMUTE, 1, "failed")]
    assert store.marked == [], "a day is only drafted once AFAS shows it"
    assert portal.cards == []
    assert "Aanmaken geklikt" in portal.alerts[0]


def test_days_already_in_afas_are_not_offered_again(cfg, monkeypatch):
    """The only card ever posted offered two days that were already Akkoord."""
    store = FakeStore(commute(SEP_1, SEP_2))
    monkeypatch.setattr(cli, "store", store)
    portal = Portal(monkeypatch, [line(SEP_1, "Akkoord")])

    assert build(cfg) == 0
    assert portal.rows == [SEP_2]
    assert store.marked[0] == ([SEP_1], DayState.SUBMITTED)
    assert portal.cards[0]["days"] == [SEP_2]
    assert portal.cards[0]["already"] == [SEP_1]


def test_everything_already_in_afas_means_no_browser_work_and_no_card(cfg, monkeypatch):
    store = FakeStore(commute(SEP_1))
    monkeypatch.setattr(cli, "store", store)
    portal = Portal(monkeypatch, [line(SEP_1, "Leidinggevende")])

    assert build(cfg) == 0
    assert portal.created == 0
    assert portal.cards == []
    assert store.states == [(2026, 9, ClaimType.COMMUTE, "submitted")]


def test_the_other_claim_type_on_a_day_goes_to_a_human(cfg, monkeypatch):
    """Art. 31a lid 12: one day cannot hold both. Ambiguity is a question, not a guess."""
    store = FakeStore(commute(SEP_1, SEP_2))
    monkeypatch.setattr(cli, "store", store)
    portal = Portal(monkeypatch, [line(SEP_1, "Akkoord", ClaimType.HOME)])

    assert build(cfg) == 1, "the rest of the month went ahead, and a human still has to step in"
    assert store.marked[0] == ([SEP_1], DayState.NEEDS_INPUT)
    assert portal.rows == [SEP_2]
    assert "01-09-2026" in portal.alerts[0]


def test_filed_days_beside_a_rejected_one_do_not_mark_the_month_as_sent(cfg, monkeypatch):
    store = FakeStore(commute(SEP_1, SEP_2))
    monkeypatch.setattr(cli, "store", store)
    Portal(monkeypatch, [line(SEP_1, "Akkoord"), line(SEP_2, "Afgewezen")])

    assert build(cfg) == 1
    assert store.states == []
    assert ([SEP_2], DayState.NEEDS_INPUT) in store.marked


def test_a_matching_draft_is_kept_not_made_twice(cfg, monkeypatch):
    """A crash between Aanmaken and the ledger write must not double the draft."""
    store = FakeStore(commute(SEP_1))
    monkeypatch.setattr(cli, "store", store)
    portal = Portal(monkeypatch, [line(SEP_1, "Medewerker")])

    assert build(cfg) == 0
    assert portal.created == 0
    assert portal.cards[0]["days"] == [SEP_1]
    assert store.upserts == [(2026, 9, ClaimType.COMMUTE, 1, "awaiting_approval")]


def test_a_draft_that_differs_from_the_ledger_fails_loudly(cfg, monkeypatch):
    store = FakeStore(commute(SEP_1, SEP_2))
    monkeypatch.setattr(cli, "store", store)
    portal = Portal(monkeypatch, [line(SEP_1, "Medewerker")])

    assert build(cfg) == 1
    assert portal.created == 0
    assert store.upserts == [(2026, 9, ClaimType.COMMUTE, 2, "failed")]
    assert "klopt niet" in portal.alerts[0]


def test_another_open_draft_makes_this_one_wait(cfg, monkeypatch):
    """A second draft of a type would block both, because tasks cannot be told apart."""
    store = FakeStore(commute(SEP_1))
    monkeypatch.setattr(cli, "store", store)
    portal = Portal(monkeypatch, [line(JUNE, "Medewerker")])

    assert build(cfg) == 1, "a month that waits is a skipped month, so the job says so"
    assert portal.created == 0
    assert store.upserts == []
    assert "02-06-2026" in portal.alerts[0]

    assert build(cfg) == 1
    assert len(portal.alerts) == 1, "said once, it stays true until a human acts"


def test_an_alert_slack_did_not_take_is_said_again_on_the_next_run(cfg, monkeypatch):
    store = FakeStore(commute(SEP_1))
    monkeypatch.setattr(cli, "store", store)
    portal = Portal(monkeypatch, [line(JUNE, "Medewerker")])
    posted = []
    monkeypatch.setattr(cli, "alert", lambda cfg, text: bool(posted.append(text)))

    assert build(cfg) == 1
    assert build(cfg) == 1
    assert len(posted) == 2, "the post failed both times, so nobody has been told yet"

    monkeypatch.setattr(cli, "alert", lambda cfg, text: portal.alerts.append(text) or True)
    assert build(cfg) == 1
    assert build(cfg) == 1
    assert len(portal.alerts) == 1


def test_a_dry_run_rehearses_the_form_and_saves_nothing(cfg, monkeypatch):
    cfg.dry_run = True
    store = FakeStore(commute(SEP_1))
    monkeypatch.setattr(cli, "store", store)
    portal = Portal(monkeypatch, [line(JUNE, "Medewerker")])

    assert build(cfg) == 0
    assert portal.rows == [SEP_1], "the real form still gets exercised"
    assert portal.rehearsed == 1
    assert portal.created == 0
    assert store.marked == []
    assert store.upserts == [(2026, 9, ClaimType.COMMUTE, 1, "drafted")]
    assert portal.cards[0]["open_drafts"] == [JUNE]


def test_one_broken_form_does_not_cost_the_other_claim_type_its_month(cfg, monkeypatch):
    days = {
        (2026, 9, ClaimType.COMMUTE): [SEP_1],
        (2026, 9, ClaimType.HOME): [SEP_2],
    }
    store = FakeStore(days)
    monkeypatch.setattr(cli, "store", store)
    portal = Portal(monkeypatch)
    portal.claim_type = ClaimType.HOME

    fill = portal._fill

    def broken_commute(cfg, page, path, rows):
        if path == cfg.commute_page:
            raise RuntimeError("form moved")
        fill(cfg, page, path, rows)

    monkeypatch.setattr(cli, "_fill_rows", broken_commute)

    assert build(cfg) == 1
    assert "form moved" in portal.alerts[0]
    assert store.upserts == [(2026, 9, ClaimType.HOME, 1, "awaiting_approval")]


def plan(cfg, store, monkeypatch, **kw):
    monkeypatch.setattr(cli, "store", store)
    said = []
    monkeypatch.setattr(cli, "alert", lambda cfg, text: said.append(text) or True)
    return cli._plan_build(cfg, object(), [(2026, 9)]), said


def submission(store, days, state, digest=None):
    return {
        (2026, 9, ClaimType.COMMUTE): {
            "lines_digest": digest or store.lines_digest(days, ClaimType.COMMUTE),
            "state": state,
        }
    }


@pytest.mark.parametrize("state", ["awaiting_approval", "approved", "rejected"])
def test_a_card_waiting_on_a_human_is_left_alone(cfg, monkeypatch, state):
    store = FakeStore(commute(SEP_1))
    store._submissions = submission(store, [SEP_1], state)
    work, _ = plan(cfg, store, monkeypatch)
    assert work == []


def test_a_failed_build_is_tried_again(cfg, monkeypatch):
    store = FakeStore(commute(SEP_1))
    store._submissions = submission(store, [SEP_1], "failed")
    work, _ = plan(cfg, store, monkeypatch)
    assert [w[:3] for w in work] == [(2026, 9, ClaimType.COMMUTE)]


def test_a_rehearsal_does_not_count_once_live(cfg, monkeypatch):
    store = FakeStore(commute(SEP_1))
    store._submissions = submission(store, [SEP_1], "drafted")

    cfg.dry_run = True
    work, _ = plan(cfg, store, monkeypatch)
    assert work == [], "no daily sign-in to repeat a rehearsal"

    cfg.dry_run = False
    work, _ = plan(cfg, store, monkeypatch)
    assert len(work) == 1


def test_changed_days_get_a_fresh_card(cfg, monkeypatch):
    store = FakeStore(commute(SEP_1, SEP_2))
    store._submissions = submission(store, [SEP_1], "awaiting_approval")
    work, _ = plan(cfg, store, monkeypatch)
    assert len(work) == 1


def test_new_days_in_a_filed_period_are_flagged_once_not_filed(cfg, monkeypatch):
    store = FakeStore(commute(SEP_2))
    store._submissions = submission(store, [SEP_1], "submitted")
    work, said = plan(cfg, store, monkeypatch)
    assert work == []
    assert "02-09-2026" in said[0]

    work, said = plan(cfg, store, monkeypatch)
    assert said == []


def test_a_period_with_only_open_questions_is_said_out_loud(cfg, monkeypatch):
    store = FakeStore(unresolved=[SEP_1])
    work, said = plan(cfg, store, monkeypatch)
    assert work == []
    assert "01-09-2026" in said[0]


def test_nothing_due_means_no_sign_in(cfg, monkeypatch):
    store = FakeStore(periods=[])
    monkeypatch.setattr(cli, "store", store)
    portal = Portal(monkeypatch)

    assert cli.cmd_build(cfg, argparse.Namespace(period=None)) == 0
    assert portal.sessions == 0


def test_a_due_period_with_no_days_at_all_alerts(cfg, monkeypatch):
    store = FakeStore(periods=[])
    store.days_in_period = lambda conn, y, p: []
    monkeypatch.setattr(cli, "store", store)
    portal = Portal(monkeypatch)

    assert cli.cmd_build(cfg, argparse.Namespace(period=None)) == 0
    assert "classify" in portal.alerts[0]


def test_a_held_lock_is_a_failed_run_and_alerts(cfg, monkeypatch):
    store = FakeStore(commute(SEP_1), lock=False)
    monkeypatch.setattr(cli, "store", store)
    portal = Portal(monkeypatch)

    assert build(cfg) == 1
    assert portal.sessions == 0
    assert "lock" in portal.alerts[0]
