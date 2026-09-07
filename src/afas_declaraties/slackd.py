"""The Slack Socket Mode handler.

Socket Mode is used because the cluster has no public ingress for this app and
does not need one: the pod dials out to Slack. That also means no signing
secret, no request URL and no tunnel.

This process deliberately never drives a browser. It writes decisions to
Postgres and the ``submit`` job does the AFAS work, which keeps the long-lived
WebSocket process small and restartable -- and means a compromised or wedged
Slack handler cannot file a declaration by itself.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from pathlib import Path

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from . import store
from .config import Config
from .models import ClaimType
from .slack_blocks import (
    APPROVE,
    CORRECT,
    MODAL_CALLBACK,
    REJECT,
    SEP,
    corrections_modal,
    parse_decision_value,
)

logger = logging.getLogger(__name__)

HEARTBEAT = Path("/tmp/healthy")

#: How often the socket is checked and the heartbeat rewritten. Must stay well
#: under the probe's max age (``slackd.heartbeatMaxAgeSeconds``, 180s by
#: default) so that one slow loop is not mistaken for a dead process.
HEARTBEAT_INTERVAL_SECONDS = 30

_CHOICE_TO_CLAIM = {
    "office": ClaimType.COMMUTE,
    "home": ClaimType.HOME,
    "absent": None,
}


def _touch_heartbeat() -> None:
    """Liveness without an HTTP server.

    The probe checks this file's age, so a handler that is running but has
    silently lost its socket still fails the probe and gets restarted.
    """
    try:
        HEARTBEAT.write_text(str(time.time()))
    except OSError:  # pragma: no cover - a read-only /tmp is a deploy bug
        logger.warning("slackd: could not write the heartbeat file")


def build_app(cfg: Config) -> App:
    cfg.require_slack()
    app = App(token=cfg.slack_bot_token, raise_error_for_unhandled_request=False)

    def authorised(user_id: str) -> bool:
        # Case-sensitive on purpose: Slack ids are, and lowercasing them would
        # silently widen the allowlist.
        return user_id in cfg.approver_ids

    def once(body: dict, kind: str, actor: str) -> bool:
        """False if Slack redelivered this interaction.

        Socket Mode replays on reconnect. Without this, a reconnect during an
        approval could approve the same period twice.
        """
        key = f"{kind}:{body.get('trigger_id') or body.get('container', {}).get('message_ts')}"
        with store.connect(cfg.database_url) as conn:
            return store.claim_slack_interaction(conn, key, kind, actor)

    @app.action(APPROVE)
    def handle_approve(ack, body, client, logger_=logger):
        ack()
        _touch_heartbeat()
        user = body["user"]["id"]
        raw = body["actions"][0]["value"]
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]

        if not authorised(user):
            client.chat_postEphemeral(
                channel=channel, user=user, text="Je staat niet op de goedkeurderslijst."
            )
            logger_.warning("slackd: unauthorised approval attempt by %s", user)
            return
        if not once(body, "approve", user):
            logger_.info("slackd: duplicate approval delivery ignored")
            return

        try:
            year, period, claim_type, digest = parse_decision_value(raw)
        except ValueError:
            logger_.error("slackd: malformed decision value %r", raw)
            client.chat_postEphemeral(
                channel=channel, user=user, text="Ongeldige knopwaarde; niets gedaan."
            )
            return

        with store.connect(cfg.database_url) as conn:
            ok, message = store.approve_submission(conn, year, period, claim_type, digest, user)

        if ok:
            # Buttons are removed on any terminal decision so a stale card can
            # never be clicked a second time.
            client.chat_update(
                channel=channel,
                ts=ts,
                text=f":white_check_mark: Goedgekeurd door <@{user}> — {message}",
                blocks=[],
            )
            client.chat_postMessage(
                channel=channel, thread_ts=ts, text="Wordt ingestuurd bij de volgende submit-run."
            )
        else:
            client.chat_postEphemeral(
                channel=channel, user=user, text=f":warning: Niet goedgekeurd: {message}"
            )

    @app.action(REJECT)
    def handle_reject(ack, body, client):
        ack()
        _touch_heartbeat()
        user, raw = body["user"]["id"], body["actions"][0]["value"]
        if not authorised(user):
            client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=user,
                text="Je staat niet op de goedkeurderslijst.",
            )
            return
        if not once(body, "reject", user):
            return
        year, period, claim_type, _ = parse_decision_value(raw)
        with store.connect(cfg.database_url) as conn:
            store.set_submission_state(conn, year, period, claim_type, "rejected")
        client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text=f":x: Afgewezen door <@{user}>. Er wordt niets ingediend.",
            blocks=[],
        )

    @app.action(CORRECT)
    def handle_correct(ack, body, client):
        """Open the day-correction modal.

        views.open needs a trigger_id, which only arrives inside an interaction
        payload -- which is why owning the interaction stream and being able to
        offer corrections are the same requirement.
        """
        ack()
        _touch_heartbeat()
        user = body["user"]["id"]
        if not authorised(user):
            client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=user,
                text="Je staat niet op de goedkeurderslijst.",
            )
            return
        raw = body["actions"][0]["value"]
        with store.connect(cfg.database_url) as conn:
            if raw.startswith(f"week{SEP}"):
                start = date.fromisoformat(raw.split(SEP, 1)[1])
                rows = store.days_between(conn, start, start + timedelta(days=6))
                meta = raw
            else:
                year, period, _claim, _digest = parse_decision_value(raw)
                rows = store.days_in_period(conn, year, period)
                meta = f"period{SEP}{year}{SEP}{period}"
        client.views_open(
            trigger_id=body["trigger_id"], view=corrections_modal(rows, private_metadata=meta)
        )

    @app.view(MODAL_CALLBACK)
    def handle_corrections(ack, body, view, client, logger_=logger):
        user = body["user"]["id"]
        if not authorised(user):
            ack(
                response_action="errors",
                errors={list(view["state"]["values"])[0]: "Je mag geen dagen corrigeren."},
            )
            return
        if not once(body, "correct", user):
            # Re-applying the same overrides would land the same values, but the
            # channel would get a second "N dagen bijgewerkt" and the ledger a
            # second overridden_at. Same guard as the approval path.
            ack()
            logger_.info("slackd: duplicate corrections delivery ignored")
            return

        # Ack before the writes, not after. Slack drops a view_submission that
        # is not acked within three seconds and redelivers it, and a slow
        # database is exactly when that happens.
        ack()
        _touch_heartbeat()

        applied, refused = [], []
        with store.connect(cfg.database_url) as conn:
            for block_id, payload in view["state"]["values"].items():
                selected = (payload.get("choice") or {}).get("selected_option")
                if not selected:
                    continue  # untouched day: leave it exactly as it was
                day = date.fromisoformat(block_id.split(SEP, 1)[1])
                claim = _CHOICE_TO_CLAIM[selected["value"]]
                (applied if store.apply_override(conn, day, claim, user) else refused).append(day)

        summary = f"{len(applied)} dag(en) bijgewerkt door <@{user}>."
        if refused:
            summary += " Niet gewijzigd (al ingediend): " + ", ".join(
                d.strftime("%d-%m") for d in refused
            )
        client.chat_postMessage(channel=cfg.slack_channel, text=summary)

    return app


def run(cfg: Config) -> None:
    """Hold the socket. Kubernetes restarts us if the heartbeat goes stale.

    ``connect()`` rather than ``start()``: start() blocks forever, so the only
    heartbeats would be the ones the interaction handlers write, and on a quiet
    week there are none. The probe would then kill a healthy process every few
    minutes -- which is exactly what it did.

    Refreshing on a timer instead does not weaken the check, because the
    refresh is conditional on the socket still being up. A handler that is
    running while Slack has stopped talking to it stops writing the file and
    gets restarted, which is the case the probe exists for.
    """
    app = build_app(cfg)
    handler = SocketModeHandler(app, cfg.slack_app_token)
    logger.info("slackd: connecting (dry_run=%s, approvers=%d)", cfg.dry_run, len(cfg.approver_ids))
    handler.connect()
    _touch_heartbeat()

    while True:
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)
        if handler.client.is_connected():
            _touch_heartbeat()
        else:
            # Deliberately no reconnect here: the SDK already retries in the
            # background. Withholding the heartbeat is how a socket that never
            # comes back becomes a restart rather than a silent outage.
            logger.warning("slackd: socket is down, withholding the heartbeat")
