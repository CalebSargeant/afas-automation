"""Telling a human.

Straight to Slack with nothing else in the way, because the failures that matter
most are the ones where Postgres or the portal is down. The cluster's own CronJob
alert only fires after two missed intervals, which for a monthly job is 62 days.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def notify(cfg, text: str) -> bool:
    """Post to the claims channel. Never raises: the caller's exit code still counts."""
    if not cfg.slack_bot_token or not cfg.slack_channel:
        logger.error("no Slack channel configured, nobody was told: %s", text)
        return False
    try:
        from slack_sdk import WebClient

        WebClient(token=cfg.slack_bot_token).chat_postMessage(channel=cfg.slack_channel, text=text)
    except Exception:  # noqa: BLE001
        logger.exception("could not post to Slack: %s", text)
        return False
    return True


def alert(cfg, text: str) -> bool:
    """Something failed or was skipped. Mentions the approvers, so it reaches a phone."""
    logger.error("ALERT: %s", text)
    who = " ".join(f"<@{user}>" for user in sorted(cfg.approver_ids))
    return notify(cfg, f":rotating_light: afas-declaraties: {text} {who}".rstrip())
