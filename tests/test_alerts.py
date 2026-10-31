"""A job that fails or skips has to reach a person.

The cluster's CronJob alert fires after two missed intervals, which on a monthly
schedule is 62 days. Two builds failed in a row and nobody heard about either.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from afas_declaraties import alerts, cli


class FakeClient:
    posted: list[dict] = []
    error: Exception | None = None

    def __init__(self, token):
        pass

    def chat_postMessage(self, **kw):
        if FakeClient.error:
            raise FakeClient.error
        FakeClient.posted.append(kw)


@pytest.fixture
def slack(monkeypatch):
    import slack_sdk

    FakeClient.posted, FakeClient.error = [], None
    monkeypatch.setattr(slack_sdk, "WebClient", FakeClient)
    return FakeClient


def cfg(**kw):
    values = {
        "slack_bot_token": "token",
        "slack_channel": "C0",
        "approver_ids": frozenset({"U2", "U1"}),
    }
    return SimpleNamespace(**(values | kw))


def test_an_alert_mentions_the_approvers(slack):
    assert alerts.alert(cfg(), "het ging mis") is True
    text = slack.posted[0]["text"]
    assert "het ging mis" in text
    assert "<@U1> <@U2>" in text
    assert slack.posted[0]["channel"] == "C0"


def test_a_notification_mentions_nobody(slack):
    alerts.notify(cfg(), "gelukt")
    assert slack.posted[0]["text"] == "gelukt"


def test_a_slack_outage_does_not_hide_the_original_failure(slack):
    slack.error = RuntimeError("slack is down")
    assert alerts.alert(cfg(), "het ging mis") is False


def test_no_channel_is_reported_not_raised(slack):
    assert alerts.alert(cfg(slack_channel=""), "het ging mis") is False
    assert slack.posted == []


@pytest.fixture
def env(monkeypatch):
    for key, value in {
        "DATABASE_URL": "postgresql://x/y",
        "INSITE_HOST": "portal.example.invalid",
        "COMMUTE_PAGE_PATH": "/a",
        "HOME_PAGE_PATH": "/b",
        "SLACK_BOT_TOKEN": "token",
        "SLACK_APP_TOKEN": "app",
        "SLACK_CHANNEL_ID": "C0",
        "SLACK_APPROVER_IDS": "U1",
        "DRY_RUN": "true",
    }.items():
        monkeypatch.setenv(key, value)
    # main() installs a SIGTERM handler. Leave the test process's own in place.
    monkeypatch.setattr(cli.signal, "signal", lambda *a: None)


@pytest.mark.parametrize("command", ["classify", "digest", "build", "submit"])
def test_a_crashing_job_alerts_and_exits_non_zero(env, slack, monkeypatch, command):
    def boom(cfg, args):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(cli, f"cmd_{command}", boom)
    assert cli.main([command]) == 1
    assert f"`{command}`" in slack.posted[0]["text"]
    assert "database is gone" in slack.posted[0]["text"]


def test_slackd_is_left_to_kubernetes(env, slack, monkeypatch):
    """It restarts in a loop when it fails, so an alert per crash would be noise."""

    def boom(cfg, args):
        raise RuntimeError("socket refused")

    monkeypatch.setattr(cli, "cmd_slackd", boom)
    with pytest.raises(RuntimeError):
        cli.main(["slackd"])
    assert slack.posted == []


def test_a_killed_job_alerts_before_it_dies(slack, monkeypatch):
    """activeDeadlineSeconds and node drains send SIGTERM, with no exception to catch."""
    exits = []
    monkeypatch.setattr(cli.os, "_exit", exits.append)
    cli._terminated(cfg(), "submit")
    assert "`submit`" in slack.posted[0]["text"]
    assert exits == [143]
