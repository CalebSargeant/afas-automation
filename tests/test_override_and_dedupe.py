"""Human corrections: what they write, and what a replay must not write.

Two bugs found by reading the ledger after a real correction went through Slack:

1. `apply_override`'s ON CONFLICT clause never listed `verdict`, so a corrected
   day kept the classifier's original opinion. A row read
   `verdict=office, claim_type=home` -- it claimed one thing and said another.
   Nothing gated on `verdict`, so no claim was wrong, but the ledger is the
   system of record and a contradictory row in it is a defect on its own.
2. The corrections handler never called `once()`, unlike the approval handler,
   so `slack_interaction` stayed empty and a Socket Mode replay would apply the
   overrides a second time.
"""

from __future__ import annotations

import inspect
from datetime import date

import pytest
from slack_bolt.kwargs_injection.args import Args

from afas_declaraties import slack_blocks, slackd, store
from afas_declaraties.models import ClaimType, Verdict

# --- what apply_override writes -------------------------------------------------


#: What the RETURNING clause yields when a row was actually written.
_UPDATED = {"day": date(2026, 9, 3)}


class FakeCursor:
    def __init__(self, returns):
        self.returns = returns
        self.sql = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params

    def fetchone(self):
        return self.returns


class FakeConn:
    def __init__(self, returns=_UPDATED):
        self.cur = FakeCursor(returns)
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


@pytest.mark.parametrize(
    "claim, expected",
    [
        (ClaimType.COMMUTE, Verdict.OFFICE),
        (ClaimType.HOME, Verdict.HOME),
        (None, Verdict.ABSENT),
    ],
)
def test_the_verdict_follows_the_claim_type(claim, expected):
    conn = FakeConn()
    assert store.apply_override(conn, date(2026, 9, 3), claim, "U1") is True
    assert expected.value in conn.cur.params


def test_every_override_verdict_is_a_real_verdict():
    """The old code wrote the literal 'human', which is not a Verdict member."""
    for value in store._OVERRIDE_VERDICT.values():
        assert Verdict(value.value) is value


def test_the_conflict_clause_updates_the_verdict():
    """The regression: without this line a corrected day keeps the old verdict."""
    conn = FakeConn()
    store.apply_override(conn, date(2026, 9, 3), ClaimType.HOME, "U1")
    update = conn.cur.sql.split("DO UPDATE SET", 1)[1]
    assert "verdict" in update and "EXCLUDED.verdict" in update


def test_human_is_no_longer_written_as_a_verdict():
    conn = FakeConn()
    store.apply_override(conn, date(2026, 9, 3), ClaimType.HOME, "U1")
    assert "'human'" not in conn.cur.sql
    assert "human" not in [p for p in conn.cur.params if isinstance(p, str)]


def test_a_submitted_day_is_refused():
    conn = FakeConn(returns=None)
    assert store.apply_override(conn, date(2026, 9, 3), ClaimType.HOME, "U1") is False


# --- the corrections handler ----------------------------------------------------


class FakeApp:
    """Captures the handlers build_app registers, so they can be called directly."""

    def __init__(self, **kw):
        self.handlers = {}

    def action(self, key):
        def wrap(fn):
            self.handlers[("action", key)] = fn
            return fn

        return wrap

    def view(self, key):
        def wrap(fn):
            self.handlers[("view", key)] = fn
            return fn

        return wrap


class FakeClient:
    def __init__(self):
        self.posted = []
        self.updated = []

    def chat_postMessage(self, **kw):
        self.posted.append(kw)

    def chat_postEphemeral(self, **kw):
        self.posted.append(kw)

    def chat_update(self, **kw):
        self.updated.append(kw)


class Cfg:
    slack_bot_token = "xoxb-x"
    slack_app_token = "xapp-x"
    slack_channel = "C1"
    database_url = "postgresql://x/y"
    dry_run = True
    approver_ids = ["U1"]

    def require_slack(self):
        return None


def view_body(user="U1", trigger="T-1"):
    return {"user": {"id": user}, "trigger_id": trigger}


def view_state(days):
    return {
        "state": {
            "values": {
                f"day{slackd.SEP}{d}": {"choice": {"selected_option": {"value": v}}}
                for d, v in days.items()
            }
        }
    }


@pytest.fixture
def app(monkeypatch):
    app = FakeApp()
    monkeypatch.setattr(slackd, "App", lambda **kw: app)

    applied: list[tuple] = []
    claimed: list[str] = []
    app.applied, app.claimed = applied, claimed

    class FakeStore:
        @staticmethod
        def connect(dsn):
            from contextlib import nullcontext

            return nullcontext(object())

        @staticmethod
        def claim_slack_interaction(conn, key, kind, actor):
            first = key not in claimed
            claimed.append(key)
            return first

        @staticmethod
        def apply_override(conn, day, claim, user):
            applied.append((day, claim, user))
            return True

        @staticmethod
        def approve_submission(conn, year, period, claim_type, digest, user):
            return True, "approved 1 day(s)"

        @staticmethod
        def reject_submission(conn, year, period, claim_type):
            return True

    monkeypatch.setattr(slackd, "store", FakeStore)
    slackd.build_app(Cfg())
    return app


@pytest.fixture
def handler(app):
    return app.handlers[("view", slackd.MODAL_CALLBACK)], app.applied, app.claimed


def test_a_correction_is_applied_once(handler):
    fn, applied, _ = handler
    acks, client = [], FakeClient()
    fn(
        ack=lambda **kw: acks.append(kw),
        body=view_body(),
        view=view_state({"2026-09-03": "home"}),
        client=client,
    )
    assert applied == [(date(2026, 9, 3), ClaimType.HOME, "U1")]
    assert len(client.posted) == 1


def test_a_replayed_submission_is_ignored(handler):
    """Socket Mode redelivers on reconnect; the same trigger_id must apply once."""
    fn, applied, _ = handler
    client = FakeClient()
    for _ in range(2):
        fn(
            ack=lambda **kw: None,
            body=view_body(trigger="T-same"),
            view=view_state({"2026-09-03": "home"}),
            client=client,
        )
    assert len(applied) == 1, "the replay applied the overrides a second time"
    assert len(client.posted) == 1, "the replay posted a second summary to the channel"


def test_an_unauthorised_user_writes_nothing(handler):
    fn, applied, claimed = handler
    errors = []
    fn(
        ack=lambda **kw: errors.append(kw),
        body=view_body(user="U-nope"),
        view=view_state({"2026-09-03": "home"}),
        client=FakeClient(),
    )
    assert applied == []
    assert claimed == [], "an unauthorised attempt must not burn the dedupe key"
    assert errors and errors[0].get("response_action") == "errors"


def test_untouched_days_are_left_alone(handler):
    fn, applied, _ = handler
    state = view_state({"2026-09-03": "home"})
    state["state"]["values"][f"day{slackd.SEP}2026-09-04"] = {"choice": {}}
    fn(ack=lambda **kw: None, body=view_body(), view=state, client=FakeClient())
    assert applied == [(date(2026, 9, 3), ClaimType.HOME, "U1")]


# --- the decision handlers ------------------------------------------------------


def card_body(user="U1"):
    blocks = slack_blocks.approval_card(
        year=2026,
        period=9,
        claim_type=ClaimType.HOME,
        days=[date(2026, 9, 1)],
        digest="d",
        unresolved=[],
        dry_run=False,
        unrecorded=[date(2026, 9, 2)],
    )
    actions = next(b for b in blocks if b["type"] == "actions")
    return {
        "user": {"id": user},
        "trigger_id": "T-card",
        "channel": {"id": "C1"},
        "message": {"ts": "1.0", "blocks": blocks},
        "actions": [{"value": actions["elements"][0]["value"]}],
    }


@pytest.mark.parametrize(
    "action, said", [(slackd.APPROVE, "Goedgekeurd"), (slackd.REJECT, "Afgewezen")]
)
def test_a_decision_swaps_only_the_buttons(app, action, said):
    """Approving once wiped the card, so the days it left out read as covered."""
    client = FakeClient()
    app.handlers[("action", action)](ack=lambda: None, body=card_body(), client=client)

    (update,) = client.updated
    assert [b["type"] for b in update["blocks"]].count("actions") == 0
    assert "Niet vastgelegd* (1): 02-09" in str(update["blocks"])
    assert said in update["blocks"][-1]["text"]["text"]


def test_handlers_ask_bolt_only_for_arguments_it_injects(app):
    """Bolt passes None for a name it does not know, whatever the default says.

    `logger_` arrived as None, so the approval handler raised on exactly the paths
    that were meant to log: an unauthorised click, a replay, a malformed value.
    """
    known = set(inspect.signature(Args).parameters)
    for key, fn in app.handlers.items():
        assert set(inspect.signature(fn).parameters) <= known, key
