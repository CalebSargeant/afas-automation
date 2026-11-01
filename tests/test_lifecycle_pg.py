"""A month through build, approval and submit, on a real Postgres.

Fakes hid the worst bug this app had: a live `build` moved its days to `drafted`,
`claimable_days` then stopped seeing them, and every approval was refused as
"changed since this card was posted". Only real rows show that.

Skipped without TEST_DATABASE_URL. The portal is faked, as everywhere in tests/.
"""

from __future__ import annotations

import argparse
import os
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import pytest

from afas_declaraties import cli, store
from afas_declaraties.classify import ClassifierConfig
from afas_declaraties.models import ClaimType, DayClassification, Reason, Verdict
from afas_declaraties.readback import FiledLine

DSN = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL is not set")

SEP_DAYS = [date(2026, 9, 3), date(2026, 9, 7)]


@pytest.fixture
def conn():
    assert "test" in DSN, "this test drops tables; point it at a throwaway database"
    with store.connect(DSN) as c:
        with c.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS claim_day, submission, slack_interaction")
        c.commit()
        store.ensure_schema(c)
        yield c


@pytest.fixture
def cfg():
    return SimpleNamespace(
        database_url=DSN,
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
    def __init__(self, monkeypatch, lines=()):
        self.lines = list(lines)
        self.claim_type = ClaimType.COMMUTE
        self.rows: list[date] = []
        self.sessions = 0
        self.alerts: list[str] = []

        @contextmanager
        def session(host, **kw):
            self.sessions += 1
            yield (object(), object())

        def create(page, *, dry_run):
            if not dry_run:
                self.lines += [FiledLine(d, "Medewerker", self.claim_type) for d in self.rows]
            return not dry_run

        def fill(cfg, page, path, days):
            self.claim_type = ClaimType.COMMUTE if path == cfg.commute_page else ClaimType.HOME
            self.rows = list(days)

        def click(page, dry_run):
            self.lines = [FiledLine(ln.day, "Leidinggevende", ln.claim_type) for ln in self.lines]
            return True

        monkeypatch.setattr(cli, "open_session", session)
        monkeypatch.setattr(cli, "read_lines", lambda *a, **kw: list(self.lines))
        monkeypatch.setattr(cli, "_fill_rows", fill)
        monkeypatch.setattr(cli, "create_draft", create)
        monkeypatch.setattr(cli, "open_task_for", lambda *a, **kw: None)
        monkeypatch.setattr(cli, "submit_declaration", click)
        monkeypatch.setattr(cli, "alert", lambda cfg, text: self.alerts.append(text))
        monkeypatch.setattr(cli, "notify", lambda cfg, text: None)
        monkeypatch.setattr(cli, "_post_card", lambda *a, **kw: None)


def office(conn, *days):
    for day in days:
        store.record_day(conn, DayClassification(day, Verdict.OFFICE, [Reason.BOOKING_PRESENT]))


def states(conn, days):
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM claim_day WHERE day = ANY(%s) ORDER BY day", (list(days),))
        return [r["state"] for r in cur.fetchall()]


def build(cfg, period="2026-09"):
    return cli.cmd_build(cfg, argparse.Namespace(period=period))


def submit(cfg):
    return cli.cmd_submit(cfg, argparse.Namespace(year=None, period=None))


def test_a_failed_calendar_read_leaves_decided_days_alone(conn, cfg, monkeypatch):
    mon, tue, wed = date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21)
    cfg.calendar_source = "mcp"
    monkeypatch.setattr(cli, "_classifier", lambda cfg: ClassifierConfig())

    def classify(since, read):
        monkeypatch.setattr(cli, "_read_calendar", lambda *a: read)
        args = argparse.Namespace(since=since.isoformat(), until=wed.isoformat(), window=9)
        assert cli.cmd_classify(cfg, args) == 0

    def days():
        return [(r["claim_type"], r["state"]) for r in store.days_between(conn, mon, wed)]

    office(conn, mon)
    store.record_day(conn, DayClassification(tue, Verdict.HOME, [Reason.NO_BOOKING]))

    classify(mon, ([], True))
    assert days() == [("commute", "planned"), ("home", "planned"), (None, "needs_input")]

    classify(wed, ([], False))
    assert days()[2] == ("home", "planned")


def test_a_live_month_goes_all_the_way_to_filed(conn, cfg, monkeypatch):
    office(conn, *SEP_DAYS)
    portal = Portal(monkeypatch)

    assert build(cfg) == 0
    assert states(conn, SEP_DAYS) == ["drafted", "drafted"]
    row = store.get_submission(conn, 2026, 9, ClaimType.COMMUTE)
    assert row["state"] == "awaiting_approval"

    # The regression: this used to refuse every real draft.
    ok, message = store.approve_submission(
        conn, 2026, 9, ClaimType.COMMUTE, row["lines_digest"], "U1"
    )
    assert ok, message

    assert submit(cfg) == 0
    assert portal.alerts == []
    assert store.get_submission(conn, 2026, 9, ClaimType.COMMUTE)["state"] == "submitted"
    assert states(conn, SEP_DAYS) == ["submitted", "submitted"]
    assert {line.status for line in portal.lines} == {"Leidinggevende"}


def test_build_is_idempotent_when_run_every_day(conn, cfg, monkeypatch):
    office(conn, *SEP_DAYS)
    portal = Portal(monkeypatch)

    assert build(cfg) == 0
    assert build(cfg) == 0
    assert portal.sessions == 1, "a card is out, so the second run has no reason to sign in"
    assert len(portal.lines) == 2

    row = store.get_submission(conn, 2026, 9, ClaimType.COMMUTE)
    store.approve_submission(conn, 2026, 9, ClaimType.COMMUTE, row["lines_digest"], "U1")
    assert submit(cfg) == 0
    assert build(cfg) == 0
    assert portal.sessions == 2, "only submit signed in again"


def test_a_stale_card_for_days_already_filed_cannot_be_approved(conn, cfg, monkeypatch):
    """The real one: 27-08 and 31-08 were on a card, and Akkoord in AFAS since September."""
    days = [date(2026, 8, 27), date(2026, 8, 31)]
    office(conn, *days)
    stale = store.lines_digest(days, ClaimType.COMMUTE)
    store.upsert_submission(conn, 2026, 8, ClaimType.COMMUTE, stale, 2, "drafted")
    portal = Portal(monkeypatch, [FiledLine(d, "Akkoord", ClaimType.COMMUTE) for d in days])

    assert build(cfg, "2026-08") == 0
    assert states(conn, days) == ["submitted", "submitted"]
    assert portal.rows == []

    ok, _ = store.approve_submission(conn, 2026, 8, ClaimType.COMMUTE, stale, "U1")
    assert not ok
    assert submit(cfg) == 0
    assert len(portal.lines) == 2


def test_a_correction_after_the_draft_voids_the_approval(conn, cfg, monkeypatch):
    office(conn, *SEP_DAYS)
    portal = Portal(monkeypatch)
    assert build(cfg) == 0
    row = store.get_submission(conn, 2026, 9, ClaimType.COMMUTE)

    store.apply_override(conn, SEP_DAYS[0], None, actor="U1")

    ok, _ = store.approve_submission(conn, 2026, 9, ClaimType.COMMUTE, row["lines_digest"], "U1")
    assert not ok

    # The draft still holds the removed day. It stays a draft until a human fixes it.
    assert build(cfg) == 1
    assert "klopt niet" in portal.alerts[0]
    assert store.get_submission(conn, 2026, 9, ClaimType.COMMUTE)["state"] == "failed"
    assert {line.status for line in portal.lines} == {"Medewerker"}


def test_dry_run_then_live_builds_for_real(conn, cfg, monkeypatch):
    office(conn, *SEP_DAYS)
    portal = Portal(monkeypatch)

    cfg.dry_run = True
    assert build(cfg) == 0
    assert portal.lines == []
    assert states(conn, SEP_DAYS) == ["planned", "planned"]
    row = store.get_submission(conn, 2026, 9, ClaimType.COMMUTE)
    assert row["state"] == "drafted"

    # A click on a card still in Slack from a rehearsal must not count once live.
    ok, _ = store.approve_submission(conn, 2026, 9, ClaimType.COMMUTE, row["lines_digest"], "U1")
    assert not ok
    assert not store.reject_submission(conn, 2026, 9, ClaimType.COMMUTE)
    assert submit(cfg) == 0
    assert portal.lines == []
    assert store.get_submission(conn, 2026, 9, ClaimType.COMMUTE)["state"] == "drafted"

    cfg.dry_run = False
    assert cli.cmd_build(cfg, argparse.Namespace(period="2026-09")) == 0
    assert len(portal.lines) == 2
    row = store.get_submission(conn, 2026, 9, ClaimType.COMMUTE)
    assert row["state"] == "awaiting_approval"
    ok, message = store.approve_submission(
        conn, 2026, 9, ClaimType.COMMUTE, row["lines_digest"], "U1"
    )
    assert ok, message


def test_a_live_approval_caught_by_a_flip_back_to_dry_run_files_nothing(conn, cfg, monkeypatch):
    office(conn, *SEP_DAYS)
    portal = Portal(monkeypatch)
    assert build(cfg) == 0
    row = store.get_submission(conn, 2026, 9, ClaimType.COMMUTE)
    assert store.approve_submission(conn, 2026, 9, ClaimType.COMMUTE, row["lines_digest"], "U1")[0]

    cfg.dry_run = True
    assert submit(cfg) == 0
    assert {line.status for line in portal.lines} == {"Medewerker"}
    assert store.get_submission(conn, 2026, 9, ClaimType.COMMUTE)["state"] == "drafted"


def test_a_stale_reject_cannot_rewrite_what_has_gone_in(conn, cfg, monkeypatch):
    office(conn, *SEP_DAYS)
    portal = Portal(monkeypatch)
    assert build(cfg) == 0
    row = store.get_submission(conn, 2026, 9, ClaimType.COMMUTE)

    assert store.reject_submission(conn, 2026, 9, ClaimType.COMMUTE)
    ok, message = store.approve_submission(
        conn, 2026, 9, ClaimType.COMMUTE, row["lines_digest"], "U1"
    )
    assert ok, message

    for state in ("in_flight", "needs_reconciliation", "submitted"):
        store.set_submission_state(conn, 2026, 9, ClaimType.COMMUTE, state)
        assert not store.reject_submission(conn, 2026, 9, ClaimType.COMMUTE)
        assert store.get_submission(conn, 2026, 9, ClaimType.COMMUTE)["state"] == state
    assert portal.alerts == []


def test_said_once_means_the_post_went_through(conn, cfg, monkeypatch):
    posts = []
    monkeypatch.setattr(cli, "alert", lambda cfg, text: bool(posts.append(text)))
    cli._alert_once(cfg, conn, "k", "t")
    cli._alert_once(cfg, conn, "k", "t")
    assert len(posts) == 2

    monkeypatch.setattr(cli, "alert", lambda cfg, text: posts.append(text) or True)
    cli._alert_once(cfg, conn, "k", "t")
    cli._alert_once(cfg, conn, "k", "t")
    assert len(posts) == 3


def test_a_click_that_died_midway_settles_from_afas(conn, cfg, monkeypatch):
    office(conn, *SEP_DAYS)
    portal = Portal(monkeypatch)
    assert build(cfg) == 0
    store.set_submission_state(conn, 2026, 9, ClaimType.COMMUTE, "in_flight")
    portal.lines = [FiledLine(d, "Leidinggevende", ClaimType.COMMUTE) for d in SEP_DAYS]

    assert submit(cfg) == 0
    assert store.get_submission(conn, 2026, 9, ClaimType.COMMUTE)["state"] == "submitted"
    assert states(conn, SEP_DAYS) == ["submitted", "submitted"]


def test_open_periods_walks_the_year_boundary_in_order(conn):
    office(conn, date(2026, 11, 2), date(2026, 12, 1), date(2027, 1, 4), date(2027, 2, 1))
    assert store.open_periods(conn, 2027, 1) == [(2026, 11), (2026, 12), (2027, 1)]
