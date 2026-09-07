"""Command line entrypoint. One image, one entrypoint, several schedules."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

from . import calendar_mcp, store
from .calendar_owa import read_week
from .classify import ClassifierConfig, classify_day
from .config import Config, ConfigError
from .insite import (
    ClaimLine,
    add_row,
    confirm_row,
    create_draft,
    fill_row,
    open_task_for,
    submit_declaration,
)
from .models import ClaimType, DayState, Verdict
from .session import open_session
from .slack_blocks import approval_card, weekly_digest

logger = logging.getLogger("afas")


def _classifier(cfg: Config) -> ClassifierConfig:
    return ClassifierConfig(
        booking_organisers=cfg.booking_organisers,
        booking_subject_prefixes=cfg.booking_prefixes,
        excluded_dates=cfg.excluded_dates,
    )


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


@contextlib.contextmanager
def _no_lock() -> Iterator[bool]:
    """Stand-in for :func:`store.browser_lock` when no browser is involved."""
    yield True


def _read_calendar(cfg: Config, start: date, end: date) -> tuple[list, bool]:
    """Collect ``start..end`` from whichever calendar source is configured.

    Both sources answer ``(events, degraded)`` and the caller cannot tell them
    apart, which is the point: the rule that a failed read must never be read as
    "no office days" has to hold whichever one is in use.
    """
    if cfg.calendar_source == "mcp":
        return calendar_mcp.read_range(
            start,
            end,
            tz=cfg.timezone,
            require_events=cfg.require_calendar_events,
        )

    with open_session(
        cfg.insite_host, profile=Path(cfg.profile_dir), item=cfg.op_item, vault=cfg.op_vault
    ) as (_ctx, page):
        events: list = []
        degraded = False
        week = _monday(start)
        while week <= end:
            week_events, week_degraded = read_week(page, week)
            events.extend(week_events)
            degraded = degraded or week_degraded
            week += timedelta(days=7)
    return events, degraded


def _slack(cfg: Config):
    from slack_sdk import WebClient

    cfg.require_slack()
    return WebClient(token=cfg.slack_bot_token)


def cmd_classify(cfg: Config, args) -> int:
    """Freeze days into the ledger while the evidence is still true.

    Runs nightly over a trailing window rather than only yesterday, so a day
    whose booking was cancelled after it was first seen gets reconsidered --
    that reconciliation is what turns "I deleted the calendar entry" into a
    question instead of a silently wrong claim.
    """
    end = date.fromisoformat(args.until) if args.until else date.today() - timedelta(days=1)
    start = date.fromisoformat(args.since) if args.since else end - timedelta(days=args.window)
    cc = _classifier(cfg)

    with store.connect(cfg.database_url) as conn:
        store.ensure_schema(conn)
        # The lock exists to stop two corporate SSO sign-ins running at once,
        # which is how an account gets locked out. The MCP reader performs no
        # sign-in and drives no browser, so it does not queue behind a browser
        # job -- and a build stuck in an SSO flow no longer blocks the nightly
        # classification behind it.
        needs_browser = cfg.calendar_source != "mcp"
        with store.browser_lock(conn) if needs_browser else _no_lock() as got:
            if not got:
                logger.warning("classify: another browser job holds the lock; exiting")
                return 0

            existing = {r["day"]: r for r in store.days_between(conn, start, end)}
            events, degraded = _read_calendar(cfg, start, end)

            if degraded:
                # Better to record nothing than to record "no bookings".
                logger.error("classify: calendar degraded; recording days as needing input")

            day = start
            counts: dict[str, int] = {}
            while day <= end:
                prior = existing.get(day)
                # Only an office verdict matters here: it is the one whose
                # disappearance from the calendar needs a human to adjudicate.
                previous_verdict = (
                    Verdict.OFFICE
                    if prior and prior.get("claim_type") == ClaimType.COMMUTE.value
                    else None
                )
                result = classify_day(
                    day,
                    [e for e in events if e.day == day],
                    config=cc,
                    calendar_degraded=degraded,
                    previous_verdict=previous_verdict,
                )
                store.record_day(conn, result)
                counts[result.verdict.value] = counts.get(result.verdict.value, 0) + 1
                day += timedelta(days=1)

    logger.info("classify: %s..%s -> %s", start, end, counts)
    return 0


def cmd_digest(cfg: Config, args) -> int:
    """Post the weekly review, so mistakes surface while they are still memorable."""
    start = _monday(
        date.fromisoformat(args.week) if args.week else date.today() - timedelta(days=7)
    )
    with store.connect(cfg.database_url) as conn:
        rows = store.days_between(conn, start, start + timedelta(days=6))
    if not rows:
        logger.info("digest: nothing recorded for the week of %s", start)
        return 0
    _slack(cfg).chat_postMessage(
        channel=cfg.slack_channel,
        text=f"Weekoverzicht {start:%d-%m-%Y}",
        blocks=weekly_digest(rows, week_start=start),
    )
    logger.info("digest: posted for week of %s (%d days)", start, len(rows))
    return 0


def cmd_build(cfg: Config, args) -> int:
    """Fill the InSite forms for a period and stop at the reversible draft.

    Aanmaken creates a task in the employee's own list; it is not the
    submission. The irreversible step happens only after a Slack approval.
    """
    today = date.today()
    target = (
        date.fromisoformat(args.period + "-01")
        if args.period
        else (today.replace(day=1) - timedelta(days=1))
    )
    year, period = target.year, target.month

    pages = {ClaimType.COMMUTE: cfg.commute_page, ClaimType.HOME: cfg.home_page}
    with store.connect(cfg.database_url) as conn:
        store.ensure_schema(conn)
        unresolved = [r["day"] for r in store.unresolved_days(conn, year, period)]
        plan = {
            ct: [r["day"] for r in store.claimable_days(conn, year, period, ct)] for ct in ClaimType
        }

        if unresolved:
            logger.warning(
                "build: %d unresolved day(s) will NOT be claimed: %s",
                len(unresolved),
                ", ".join(d.isoformat() for d in unresolved),
            )

        with store.browser_lock(conn) as got:
            if not got:
                logger.warning("build: another browser job holds the lock; exiting")
                return 0
            with open_session(
                cfg.insite_host, profile=Path(cfg.profile_dir), item=cfg.op_item, vault=cfg.op_vault
            ) as (_ctx, page):
                for claim_type, days in plan.items():
                    if not days:
                        continue
                    page.goto(
                        f"https://{cfg.insite_host}{pages[claim_type]}",
                        wait_until="domcontentloaded",
                        timeout=90_000,
                    )
                    page.wait_for_timeout(5_000)
                    for day in days:
                        window = add_row(page)
                        fill_row(page, window, ClaimLine(day))
                        confirm_row(page, window)
                    created = create_draft(page, dry_run=cfg.dry_run)

                    digest = store.lines_digest(days, claim_type)
                    state = "awaiting_approval" if created else "drafted"
                    store.upsert_submission(
                        conn, year, period, claim_type, digest, len(days), state
                    )
                    if created:
                        store.mark_days_state(conn, days, DayState.DRAFTED)
                    logger.info(
                        "build: %s %02d/%d -> %d line(s), state=%s",
                        claim_type.value,
                        period,
                        year,
                        len(days),
                        state,
                    )

                    if cfg.slack_channel:
                        _slack(cfg).chat_postMessage(
                            channel=cfg.slack_channel,
                            text=f"Declaratie {claim_type.value} {period:02d}/{year} klaar",
                            blocks=approval_card(
                                year=year,
                                period=period,
                                claim_type=claim_type,
                                days=days,
                                digest=digest,
                                unresolved=unresolved,
                                dry_run=cfg.dry_run,
                            ),
                        )
    return 0


def cmd_override(cfg: Config, args) -> int:
    """Escape hatch for when Slack is unavailable."""
    mapping = {"office": ClaimType.COMMUTE, "home": ClaimType.HOME, "absent": None}
    with store.connect(cfg.database_url) as conn:
        store.ensure_schema(conn)
        ok = store.apply_override(
            conn, date.fromisoformat(args.day), mapping[args.verdict], actor=args.actor
        )
    print(f"{args.day} -> {args.verdict}: {'applied' if ok else 'refused (already submitted)'}")
    return 0 if ok else 1


def cmd_slackd(cfg: Config, args) -> int:
    from .slackd import run

    run(cfg)
    return 0


def cmd_submit(cfg: Config, args) -> int:
    """File approved declarations with the manager. The irreversible step.

    Only acts on periods a human approved in Slack, and re-checks the approval
    digest first: if the day set moved after approval, the approval was for
    different content and this refuses rather than filing it.

    With no arguments it files everything standing in `approved`, which is what
    the CronJob runs. It deliberately does not work the period out from today's
    date: `build` runs on the 28th for the *previous* month, so a submit waking
    on the 1st would compute the month after the approved one.
    """
    with store.connect(cfg.database_url) as conn:
        store.ensure_schema(conn)

        if bool(args.year) != bool(args.period):
            logger.error("submit: --year and --period must be given together, or not at all")
            return 2

        if args.year:
            rows = [
                row
                for claim_type in ClaimType
                if (row := store.get_submission(conn, args.year, args.period, claim_type))
                and row["state"] == "approved"
            ]
        else:
            rows = store.approved_submissions(conn)

        if not rows:
            logger.info("submit: nothing approved")
            return 0

        with store.browser_lock(conn) as got:
            if not got:
                logger.warning("submit: another browser job holds the lock; exiting")
                return 0

            for row in rows:
                year, period = row["period_year"], row["period_no"]
                claim_type = ClaimType(row["claim_type"])
                days = [r["day"] for r in store.claimable_days(conn, year, period, claim_type)]
                live = store.lines_digest(days, claim_type)
                if live != row["lines_digest"]:
                    logger.error(
                        "submit: %s %02d/%d changed since approval (%s -> %s); refusing",
                        claim_type.value,
                        period,
                        year,
                        row["lines_digest"],
                        live,
                    )
                    store.set_submission_state(
                        conn,
                        year,
                        period,
                        claim_type,
                        "awaiting_approval",
                        error="digest changed after approval",
                    )
                    continue

                slug = cfg.commute_page if claim_type is ClaimType.COMMUTE else cfg.home_page
                # Written BEFORE the click. If the process dies mid-submit we
                # must not be able to conclude it never happened and retry, as
                # the portal offers no way to detect a duplicate.
                store.set_submission_state(conn, year, period, claim_type, "in_flight")
                try:
                    with open_session(
                        cfg.insite_host,
                        profile=Path(cfg.profile_dir),
                        item=cfg.op_item,
                        vault=cfg.op_vault,
                    ) as (_ctx, page):
                        open_task_for(page, f"https://{cfg.insite_host}/", slug.rsplit("/", 1)[-1])
                        sent = submit_declaration(page, dry_run=cfg.dry_run)
                except Exception as exc:  # noqa: BLE001
                    # Never auto-retry: a partial submit cannot be distinguished
                    # from no submit, and the portal has no idempotency key.
                    store.set_submission_state(
                        conn,
                        year,
                        period,
                        claim_type,
                        "needs_reconciliation",
                        error=str(exc)[:400],
                    )
                    logger.error(
                        "submit: %s failed, marked needs_reconciliation: %s", claim_type.value, exc
                    )
                    continue

                if sent:
                    store.set_submission_state(conn, year, period, claim_type, "submitted")
                    store.mark_days_state(conn, days, DayState.SUBMITTED)
                    logger.info(
                        "submit: %s %02d/%d filed (%d days)",
                        claim_type.value,
                        period,
                        year,
                        len(days),
                    )
                else:
                    store.set_submission_state(conn, year, period, claim_type, "approved")
                    logger.info(
                        "submit: DRY RUN -- %s %02d/%d left approved",
                        claim_type.value,
                        period,
                        year,
                    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="afas-declaraties")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("classify", help="freeze classified days into the ledger")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument(
        "--window", type=int, default=9, help="days back from --until when --since is absent"
    )
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("digest", help="post the weekly review to Slack")
    p.add_argument("--week", help="any date in the target week (ISO)")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("build", help="fill the InSite forms and create the draft")
    p.add_argument("--period", help="YYYY-MM (default: previous month)")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("submit", help="send approved declarations to the manager")
    p.add_argument("--year", type=int, help="with --period, file only this period")
    p.add_argument("--period", type=int, help="month number, 1-12")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("slackd", help="run the Slack Socket Mode handler")
    p.set_defaults(func=cmd_slackd)

    p = sub.add_parser("override", help="record a human decision for one day")
    p.add_argument("day")
    p.add_argument("verdict", choices=["office", "home", "absent"])
    p.add_argument("--actor", default="cli")
    p.set_defaults(func=cmd_override)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        logger.error("configuration error: %s", exc)
        return 2
    return args.func(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
