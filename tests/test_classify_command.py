"""`classify` when the calendar cannot be read.

A refused sign-in or a broken page is a day without a reading, never a crash and
never "no office days". Weeks that were read still count.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from afas_declaraties import cli
from afas_declaraties.classify import ClassifierConfig
from afas_declaraties.models import Reason

MON = date(2026, 10, 19)
NEXT_MON = MON + timedelta(days=7)
UNTIL = NEXT_MON + timedelta(days=2)


class FakeStore:
    def __init__(self, existing=()):
        self._existing = {r["day"]: r for r in existing}
        self.recorded: list = []

    @contextmanager
    def connect(self, dsn):
        yield object()

    def ensure_schema(self, conn):
        pass

    @contextmanager
    def browser_lock(self, conn):
        yield True

    def days_between(self, conn, start, end):
        return [r for d, r in self._existing.items() if start <= d <= end]

    def record_day(self, conn, result):
        self.recorded.append(result)


@pytest.fixture
def cfg():
    return SimpleNamespace(
        database_url="postgresql://x/y",
        calendar_source="owa",
        insite_host="portal.example.invalid",
        profile_dir="/tmp/profile",
        op_item="item",
        op_vault="vault",
    )


@pytest.fixture
def world(monkeypatch):
    said: list[str] = []
    monkeypatch.setattr(cli, "alert", lambda cfg, text: said.append(text) or True)
    monkeypatch.setattr(cli, "_classifier", lambda cfg: ClassifierConfig())

    @contextmanager
    def session(host, **kw):
        yield (object(), object())

    monkeypatch.setattr(cli, "open_session", session)
    return said


def classify(cfg):
    args = argparse.Namespace(since=MON.isoformat(), until=UNTIL.isoformat(), window=9)
    return cli.cmd_classify(cfg, args)


def weekdays(start, end):
    return {start + timedelta(days=n) for n in range((end - start).days + 1)} - {
        date(2026, 10, 24),
        date(2026, 10, 25),
    }


def degraded(store):
    return {r.day for r in store.recorded if Reason.CALENDAR_DEGRADED in r.reasons}


def test_a_refused_sign_in_leaves_new_days_undecided_and_says_so_once(cfg, world, monkeypatch):
    decided = {"day": MON, "claim_type": "commute"}
    store = FakeStore([decided])
    monkeypatch.setattr(cli, "store", store)

    @contextmanager
    def refused(host, **kw):
        raise RuntimeError("sign-in refused")
        yield

    monkeypatch.setattr(cli, "open_session", refused)

    assert classify(cfg) == 0
    assert MON not in {r.day for r in store.recorded}, "a decided day stays as it was"
    assert degraded(store) == weekdays(MON, UNTIL) - {MON}
    assert len(world) == 1
    assert "calendar unreadable for 19-10..28-10" in world[0]
    assert "sign-in refused" in world[0]


def test_weeks_read_before_a_failure_still_count(cfg, world, monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(cli, "store", store)
    weeks = []

    def read_week(page, monday, **kw):
        weeks.append(monday)
        if monday == NEXT_MON:
            raise RuntimeError("Outlook refused the sign-in")
        return [], False

    monkeypatch.setattr(cli, "read_week", read_week)

    assert classify(cfg) == 0
    assert weeks == [MON, NEXT_MON]
    assert degraded(store) == {NEXT_MON, NEXT_MON + timedelta(days=1), UNTIL}
    assert len(world) == 1
    assert "calendar unreadable for 26-10..28-10" in world[0]


def test_a_failure_stops_the_read_so_nothing_signs_in_twice(cfg, world, monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(cli, "store", store)
    weeks = []

    def read_week(page, monday, **kw):
        weeks.append(monday)
        raise RuntimeError("Outlook refused the sign-in")

    monkeypatch.setattr(cli, "read_week", read_week)

    assert classify(cfg) == 0
    assert weeks == [MON]
    assert degraded(store) == weekdays(MON, UNTIL)


def test_a_week_read_cleanly_is_not_degraded(cfg, world, monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(cli, "store", store)
    monkeypatch.setattr(cli, "read_week", lambda page, monday, **kw: ([], monday == NEXT_MON))

    assert classify(cfg) == 0
    assert degraded(store) == {NEXT_MON, NEXT_MON + timedelta(days=1), UNTIL}
    assert "26-10..28-10" in world[0]
