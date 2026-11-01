"""Command line entrypoint. One image, one entrypoint, several schedules."""

from __future__ import annotations

import argparse
import calendar
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Iterable, Iterator
from datetime import date, timedelta
from pathlib import Path

from . import calendar_mcp, store
from .alerts import alert, notify
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
from .readback import position, read_lines
from .session import open_session
from .slack_blocks import approval_card, weekly_digest

logger = logging.getLogger("afas")

#: A month falls due on this day of the month after it.
BUILD_DAY = 28

#: A declaration has gone to the portal; `build` must not touch it again.
_SENT_STATES = {"submitted", "in_flight", "needs_reconciliation"}
#: A card is out and the next move is a human's.
_WAITING_STATES = {"awaiting_approval", "approved", "rejected"}
#: The commands a schedule runs, so nobody is watching their terminal.
_JOBS = {"classify", "digest", "build", "submit"}


def _classifier(cfg: Config) -> ClassifierConfig:
    return ClassifierConfig(
        booking_organisers=cfg.booking_organisers,
        booking_subject_prefixes=cfg.booking_prefixes,
        excluded_dates=cfg.excluded_dates,
    )


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _dates(days: Iterable[date]) -> str:
    return ", ".join(d.strftime("%d-%m-%Y") for d in days)


@contextlib.contextmanager
def _no_lock() -> Iterator[bool]:
    """Stand-in for :func:`store.browser_lock` when no browser is involved."""
    yield True


def _lock_held(cfg: Config, command: str) -> int:
    """A skipped run is a failed run: on a monthly job it would lose a whole month."""
    alert(cfg, f"`{command}` overgeslagen, een andere browser-job houdt de lock vast.")
    return 1


def _alert_once(cfg: Config, conn, key: str, text: str) -> None:
    """For a condition that stays true until a human acts, so it is said once.

    Said means the post went through. A failed one is tried again on the next run.
    """
    key = f"alert:{key}"
    if not store.interaction_seen(conn, key) and alert(cfg, text):
        store.claim_slack_interaction(conn, key, "alert", "afas")


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
                return _lock_held(cfg, "classify")

            existing = {r["day"]: r for r in store.days_between(conn, start, end)}
            events, degraded = _read_calendar(cfg, start, end)

            if degraded:
                # Better to record nothing than to record "no bookings".
                alert(
                    cfg,
                    f"agenda niet leesbaar voor {start:%d-%m}..{end:%d-%m}. Besliste dagen "
                    "blijven staan, nieuwe dagen staan op onbeslist.",
                )

            day = start
            counts: dict[str, int] = {}
            while day <= end:
                prior = existing.get(day)
                # A failed read says nothing about a day a good read already decided.
                if degraded and prior:
                    day += timedelta(days=1)
                    continue
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
        alert(cfg, f"geen enkele dag vastgelegd voor de week van {start:%d-%m-%Y}.")
        return 0
    _slack(cfg).chat_postMessage(
        channel=cfg.slack_channel,
        text=f"Weekoverzicht {start:%d-%m-%Y}",
        blocks=weekly_digest(rows, week_start=start),
    )
    logger.info("digest: posted for week of %s (%d days)", start, len(rows))
    return 0


def _latest_due(today: date) -> tuple[int, int]:
    """The newest period `build` may work on today."""
    last_month = today.replace(day=1) - timedelta(days=1)
    if today.day < BUILD_DAY:
        last_month = last_month.replace(day=1) - timedelta(days=1)
    return last_month.year, last_month.month


def _unrecorded_weekdays(conn, year: int, period: int) -> list[date]:
    """Weekdays the ledger never saw. Not claimed, and worth saying so."""
    recorded = {r["day"] for r in store.days_in_period(conn, year, period)}
    return [
        d
        for n in range(1, calendar.monthrange(year, period)[1] + 1)
        if (d := date(year, period, n)).weekday() < 5 and d not in recorded
    ]


def _post_card(
    cfg: Config,
    conn,
    year: int,
    period: int,
    claim_type: ClaimType,
    days: list[date],
    *,
    already: list[date] | None = None,
    open_drafts: list[date] | None = None,
) -> None:
    if not cfg.slack_channel:
        return
    _slack(cfg).chat_postMessage(
        channel=cfg.slack_channel,
        text=f"Declaratie {claim_type.value} {period:02d}/{year} klaar",
        blocks=approval_card(
            year=year,
            period=period,
            claim_type=claim_type,
            days=days,
            digest=store.lines_digest(days, claim_type),
            unresolved=[r["day"] for r in store.unresolved_days(conn, year, period)],
            dry_run=cfg.dry_run,
            already=already or [],
            unrecorded=_unrecorded_weekdays(conn, year, period),
            open_drafts=open_drafts or [],
        ),
    )


def _plan_build(cfg: Config, conn, periods: list[tuple[int, int]]) -> list[tuple]:
    """What needs a browser today. Runs daily, so nearly every answer is nothing."""
    work = []
    for year, period in periods:
        planned = {
            ct: [r["day"] for r in store.claimable_days(conn, year, period, ct)] for ct in ClaimType
        }
        if not any(planned.values()):
            unresolved = [r["day"] for r in store.unresolved_days(conn, year, period)]
            if unresolved:
                _alert_once(
                    cfg,
                    conn,
                    f"unresolved:{year}-{period}:{_dates(unresolved)}",
                    f"{period:02d}/{year}: niets te declareren, deze dagen zijn nog onbeslist: "
                    f"{_dates(unresolved)}.",
                )
            continue

        for claim_type, days in planned.items():
            if not days:
                continue
            existing = store.get_submission(conn, year, period, claim_type)
            if existing:
                digest = store.lines_digest(days, claim_type)
                same = existing["lines_digest"] == digest
                state = existing["state"]
                if state in _SENT_STATES:
                    if not same:
                        _alert_once(
                            cfg,
                            conn,
                            f"late:{year}-{period}:{claim_type.value}:{digest}",
                            f"{claim_type.value} {period:02d}/{year} is al ingestuurd, maar deze "
                            f"dagen staan nog open: {_dates(days)}. Dien ze met de hand in of "
                            "corrigeer ze.",
                        )
                    continue
                # `drafted` is a dry run's rehearsal. A live run still has to do it for real.
                if same and (state in _WAITING_STATES or (state == "drafted" and cfg.dry_run)):
                    continue
            work.append((year, period, claim_type, days))
    return work


def _fill_rows(cfg: Config, page, path: str, days: list[date]) -> None:
    page.goto(f"https://{cfg.insite_host}{path}", wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(5_000)
    for day in days:
        window = add_row(page)
        fill_row(page, window, ClaimLine(day))
        confirm_row(page, window)


def _build_one(cfg: Config, conn, page, year, period, claim_type, days) -> bool:
    """One claim type for one period. True unless a human has to step in."""
    label = f"{claim_type.value} {period:02d}/{year}"
    pos = position(read_lines(page, cfg.insite_host, cfg.claims_page), claim_type, days)

    # AFAS outranks the ledger: a day filed by hand or by an earlier run is not
    # offered again. Written down, so the ledger stops proposing it.
    if pos.filed:
        store.mark_days_state(conn, pos.filed, DayState.SUBMITTED)
    if pos.conflict:
        store.mark_days_state(conn, pos.conflict, DayState.NEEDS_INPUT)
        alert(
            cfg,
            f"{label}: {_dates(pos.conflict)} staan in AFAS met een ander type of een "
            "onbekende status. Niet meegenomen, kijk in AFAS en corrigeer de dag.",
        )

    todo = sorted(pos.draft + pos.absent)
    if not todo:
        if pos.filed and not pos.conflict:
            store.set_submission_state(conn, year, period, claim_type, "submitted")
        logger.info("build: %s has nothing left to file", label)
        return not pos.conflict

    digest = store.lines_digest(todo, claim_type)
    path = cfg.commute_page if claim_type is ClaimType.COMMUTE else cfg.home_page

    if cfg.dry_run:
        # The real form, every run: the rows are entered and verified, then left unsaved.
        _fill_rows(cfg, page, path, pos.absent)
        create_draft(page, dry_run=True)
        state = "drafted"
    else:
        this_period = [d for d in pos.other_drafts if store.period_for(d) == (year, period)]
        if this_period or (pos.draft and pos.absent):
            problem = (
                f"{label}: het concept in AFAS klopt niet met het grootboek. Concept: "
                f"{_dates(pos.draft + this_period)}, ontbreekt: {_dates(pos.absent)}. "
                "Verwijder of herstel het concept in AFAS."
            )
            store.upsert_submission(
                conn, year, period, claim_type, digest, len(todo), "failed", error=problem
            )
            alert(cfg, problem)
            return False
        if pos.other_drafts:
            # The task list cannot tell two drafts of one type apart, so a second
            # would block both. This one waits its turn.
            _alert_once(
                cfg,
                conn,
                f"blocked:{claim_type.value}:{_dates(pos.other_drafts)}",
                f"{label} wacht: er staat al een open {claim_type.value}-concept in AFAS "
                f"({_dates(pos.other_drafts)}). Stuur dat in of verwijder het.",
            )
            return False
        if pos.absent:
            _fill_rows(cfg, page, path, pos.absent)
            create_draft(page, dry_run=False)
            after = position(read_lines(page, cfg.insite_host, cfg.claims_page), claim_type, todo)
            if after.draft != todo:
                problem = (
                    f"{label}: Aanmaken geklikt, maar AFAS toont als concept "
                    f"{_dates(after.draft) or 'niets'} in plaats van {_dates(todo)}."
                )
                store.upsert_submission(
                    conn, year, period, claim_type, digest, len(todo), "failed", error=problem
                )
                alert(cfg, problem)
                return False
        store.mark_days_state(conn, todo, DayState.DRAFTED)
        state = "awaiting_approval"

    _post_card(
        cfg,
        conn,
        year,
        period,
        claim_type,
        todo,
        already=pos.filed,
        open_drafts=pos.other_drafts,
    )
    store.upsert_submission(conn, year, period, claim_type, digest, len(todo), state)
    logger.info("build: %s -> %d line(s), state=%s", label, len(todo), state)
    return not pos.conflict


def cmd_build(cfg: Config, args) -> int:
    """Get each due period to a card in Slack, stopping at the reversible draft.

    Scheduled daily and does nothing most days. A month is one shot otherwise:
    two of them were lost to a crash here with nothing to pick them up. Each run
    is one sign-in, so this is a next attempt, not a retry loop.

    Aanmaken creates a task in the employee's own list; it is not the
    submission. The irreversible step happens only after a Slack approval.
    """
    with store.connect(cfg.database_url) as conn:
        store.ensure_schema(conn)
        if args.period:
            target = date.fromisoformat(args.period + "-01")
            periods = [(target.year, target.month)]
        else:
            due = _latest_due(date.today())
            periods = store.open_periods(conn, *due)
            if not store.days_in_period(conn, *due):
                _alert_once(
                    cfg,
                    conn,
                    f"empty:{due[0]}-{due[1]}",
                    f"{due[1]:02d}/{due[0]} is aan de beurt en er staat geen enkele dag in het "
                    "grootboek. Draait classify nog?",
                )

        work = _plan_build(cfg, conn, periods)
        if not work:
            logger.info("build: nothing to do")
            return 0

        ok = True
        with store.browser_lock(conn) as got:
            if not got:
                return _lock_held(cfg, "build")
            with open_session(
                cfg.insite_host, profile=Path(cfg.profile_dir), item=cfg.op_item, vault=cfg.op_vault
            ) as (_ctx, page):
                for year, period, claim_type, days in work:
                    try:
                        ok = _build_one(cfg, conn, page, year, period, claim_type, days) and ok
                    except Exception as exc:  # noqa: BLE001
                        # One broken form must not cost the other claim type its month.
                        logger.exception("build: %s %02d/%d", claim_type.value, period, year)
                        alert(
                            cfg,
                            f"`build` {claim_type.value} {period:02d}/{year} mislukt: "
                            f"{type(exc).__name__}: {str(exc)[:300]}",
                        )
                        ok = False
    return 0 if ok else 1


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


def _record_filed(cfg: Config, conn, year, period, claim_type, days) -> None:
    store.set_submission_state(conn, year, period, claim_type, "submitted")
    store.mark_days_state(conn, days, DayState.SUBMITTED)
    logger.info("submit: %s %02d/%d filed (%d days)", claim_type.value, period, year, len(days))
    notify(
        cfg,
        f":white_check_mark: AFAS bevestigt {claim_type.value} {period:02d}/{year}: "
        f"{len(days)} regel(s) staan bij de leidinggevende: {_dates(days)}.",
    )


def _settle(cfg: Config, conn, page, row: dict) -> bool:
    """A declaration whose click may or may not have landed. Reads, never clicks."""
    year, period = row["period_year"], row["period_no"]
    claim_type = ClaimType(row["claim_type"])
    label = f"{claim_type.value} {period:02d}/{year}"
    days = [r["day"] for r in store.claimable_days(conn, year, period, claim_type)]
    pos = position(read_lines(page, cfg.insite_host, cfg.claims_page), claim_type, days)

    if days and pos.filed == sorted(days):
        _record_filed(cfg, conn, year, period, claim_type, days)
        return True
    if days and pos.draft == sorted(days):
        # The card goes first: if Slack fails, this run comes back here tomorrow.
        _post_card(cfg, conn, year, period, claim_type, days, open_drafts=pos.other_drafts)
        store.set_submission_state(
            conn,
            year,
            period,
            claim_type,
            "awaiting_approval",
            error="click did not land",
            digest=store.lines_digest(days, claim_type),
        )
        alert(cfg, f"{label}: insturen is niet aangekomen, het staat nog als concept in AFAS.")
        return False
    if days and pos.absent == sorted(days):
        store.set_submission_state(
            conn, year, period, claim_type, "failed", error="draft gone from AFAS"
        )
        alert(cfg, f"{label}: het concept is weg uit AFAS. De volgende build maakt het opnieuw.")
        return False
    # A rejection lands here too. The next build reads AFAS again and clicks nothing
    # without a new approval, so `failed` is safe and this stops repeating every day.
    problem = (
        f"{label}: onduidelijke staat, kijk in AFAS. Ingestuurd: {_dates(pos.filed)}, "
        f"concept: {_dates(pos.draft)}, ontbreekt: {_dates(pos.absent)}, "
        f"anders: {_dates(pos.conflict)}."
    )
    store.set_submission_state(conn, year, period, claim_type, "failed", error=problem[:400])
    alert(cfg, problem)
    return False


def _still_as_approved(cfg: Config, conn, row: dict) -> bool:
    """An approval is for exact content. Checked before anything signs in."""
    year, period = row["period_year"], row["period_no"]
    claim_type = ClaimType(row["claim_type"])
    days = [r["day"] for r in store.claimable_days(conn, year, period, claim_type)]
    if store.lines_digest(days, claim_type) == row["lines_digest"]:
        return True
    store.set_submission_state(
        conn,
        year,
        period,
        claim_type,
        "awaiting_approval",
        error="digest changed after approval",
    )
    alert(
        cfg,
        f"{claim_type.value} {period:02d}/{year}: geweigerd, "
        "de dagen zijn veranderd na de goedkeuring.",
    )
    return False


def _file(cfg: Config, conn, page, row: dict) -> bool:
    """File one approved declaration. Success means AFAS shows it, not that a click worked."""
    year, period = row["period_year"], row["period_no"]
    claim_type = ClaimType(row["claim_type"])
    label = f"{claim_type.value} {period:02d}/{year}"
    days = [r["day"] for r in store.claimable_days(conn, year, period, claim_type)]

    pos = position(read_lines(page, cfg.insite_host, cfg.claims_page), claim_type, days)
    if pos.filed == sorted(days):
        _record_filed(cfg, conn, year, period, claim_type, days)
        return True

    if pos.draft != sorted(days):
        # What was approved is no longer there, so the approval goes with it. Left
        # standing, `build` would skip this row and nothing could ever move it.
        problem = (
            f"{label}: het concept in AFAS klopt niet met wat is goedgekeurd. "
            f"Concept: {_dates(pos.draft)}, ingestuurd: {_dates(pos.filed)}, "
            f"ontbreekt: {_dates(pos.absent)}, anders: {_dates(pos.conflict)}. "
            "De volgende build maakt een nieuwe kaart."
        )
        store.set_submission_state(conn, year, period, claim_type, "failed", error=problem)
        alert(cfg, problem)
        return False
    if pos.other_drafts:
        # The approved draft is intact, so the approval stands and tomorrow's run looks again.
        problem = (
            f"{label}: er staat nog een open concept in AFAS ({_dates(pos.other_drafts)}) en de "
            "takenlijst kan ze niet uit elkaar houden. Stuur dat in of verwijder het."
        )
        store.set_submission_state(conn, year, period, claim_type, "approved", error=problem)
        alert(cfg, problem)
        return False

    slug = cfg.commute_page if claim_type is ClaimType.COMMUTE else cfg.home_page
    # Written BEFORE the click. If the process dies mid-submit the next run has
    # to find out from AFAS what happened, and never just clicks again.
    store.set_submission_state(conn, year, period, claim_type, "in_flight")
    try:
        open_task_for(page, f"https://{cfg.insite_host}/", slug.rsplit("/", 1)[-1])
        submit_declaration(page, dry_run=False)
        after = position(read_lines(page, cfg.insite_host, cfg.claims_page), claim_type, days)
    except Exception as exc:  # noqa: BLE001
        store.set_submission_state(
            conn, year, period, claim_type, "needs_reconciliation", error=str(exc)[:400]
        )
        alert(cfg, f"{label}: insturen onderbroken, kijk in AFAS: {str(exc)[:300]}")
        return False

    if after.filed != sorted(days):
        problem = (
            f"{label}: insturen geklikt, maar AFAS bevestigt het niet. Ingestuurd: "
            f"{_dates(after.filed)}, concept: {_dates(after.draft)}, "
            f"ontbreekt: {_dates(after.absent)}."
        )
        store.set_submission_state(
            conn, year, period, claim_type, "needs_reconciliation", error=problem[:400]
        )
        alert(cfg, problem)
        return False

    _record_filed(cfg, conn, year, period, claim_type, days)
    return True


def cmd_submit(cfg: Config, args) -> int:
    """File approved declarations with the manager. The irreversible step.

    Only acts on periods a human approved in Slack, and re-checks the approval
    digest first: if the day set moved after approval, the approval was for
    different content and this refuses rather than filing it.

    With no arguments it files everything standing in `approved`, which is what
    the CronJob runs. It deliberately does not work the period out from today's
    date: `build` works on periods well behind today, so a submit waking on the
    1st would compute a different month from the one approved.
    """
    if bool(args.year) != bool(args.period):
        logger.error("submit: --year and --period must be given together, or not at all")
        return 2

    with store.connect(cfg.database_url) as conn:
        store.ensure_schema(conn)

        if args.year:
            rows = [
                row
                for claim_type in ClaimType
                if (row := store.get_submission(conn, args.year, args.period, claim_type))
                and row["state"] == "approved"
            ]
        else:
            rows = store.approved_submissions(conn)
        unsettled = store.unsettled_submissions(conn)

        if cfg.dry_run:
            # A live approval caught by a flip back to dry run. Going live again posts a new card.
            for row in rows:
                year, period = row["period_year"], row["period_no"]
                claim_type = ClaimType(row["claim_type"])
                store.set_submission_state(conn, year, period, claim_type, "drafted")
                notify(
                    cfg,
                    f":test_tube: DRY RUN: {claim_type.value} {period:02d}/{year} goedgekeurd, "
                    "er is niets ingestuurd.",
                )
            rows = []

        ready = [row for row in rows if _still_as_approved(cfg, conn, row)]
        ok = len(ready) == len(rows)
        rows = ready

        if not rows and not unsettled:
            logger.info("submit: nothing to file")
            return 0 if ok else 1

        with store.browser_lock(conn) as got:
            if not got:
                return _lock_held(cfg, "submit")
            with open_session(
                cfg.insite_host, profile=Path(cfg.profile_dir), item=cfg.op_item, vault=cfg.op_vault
            ) as (_ctx, page):
                for row in unsettled:
                    ok = _settle(cfg, conn, page, row) and ok
                for row in rows:
                    ok = _file(cfg, conn, page, row) and ok
    return 0 if ok else 1


def _terminated(cfg: Config, command: str) -> None:
    """A deadline or a drain kills the pod with no exception to catch."""
    alert(cfg, f"`{command}` is afgebroken (deadline of node weg). Kijk of AFAS klopt.")
    os._exit(143)


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
    p.add_argument("--period", help="YYYY-MM (default: every period that is due)")
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

    if args.command not in _JOBS:
        return args.func(cfg, args)

    signal.signal(signal.SIGTERM, lambda *_: _terminated(cfg, args.command))
    try:
        return args.func(cfg, args)
    except Exception as exc:  # noqa: BLE001
        logger.exception("%s failed", args.command)
        alert(cfg, f"`{args.command}` is gecrasht: {type(exc).__name__}: {str(exc)[:300]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
