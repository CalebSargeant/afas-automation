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
from .entra import sign_in
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
from .session import credentials, open_session
from .slack_blocks import approval_card, weekly_digest

logger = logging.getLogger("afas")

#: A month falls due on this day of the month after it.
BUILD_DAY = 28

#: The click may have landed. Only AFAS can say, and `submit` asks it before anyone builds.
_UNSETTLED_STATES = {"in_flight", "needs_reconciliation"}
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
    alert(cfg, f"`{command}` skipped, another browser job holds the lock.")
    return 1


def _alert_once(cfg: Config, conn, key: str, text: str, post=None) -> None:
    """For a condition that stays true until a human acts, so it is said once.

    Said means the post went through. A failed one is tried again on the next run.
    ``post`` is :func:`notify` for a condition that is expected, not a failure.
    """
    key = f"alert:{key}"
    if not store.interaction_seen(conn, key) and (post or alert)(cfg, text):
        store.claim_slack_interaction(conn, key, "alert", "afas")


def _failure(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:300]}"


def _read_calendar(cfg: Config, start: date, end: date) -> tuple[list, set[date], str]:
    """Collect ``start..end`` from whichever calendar source is configured.

    Returns the events, the days no good read covered, and what went wrong. A
    failure, a refused sign-in included, leaves days unread instead of ending the
    run, and the rule that an unread day is never "no office days" holds whichever
    source is in use.
    """
    window = {start + timedelta(days=n) for n in range((end - start).days + 1)}
    if cfg.calendar_source == "mcp":
        try:
            events, degraded = calendar_mcp.read_range(
                start,
                end,
                tz=cfg.timezone,
                require_events=cfg.require_calendar_events,
            )
        except Exception as exc:  # noqa: BLE001 - unread days, not a crash
            logger.exception("classify: calendar read failed")
            return [], window, _failure(exc)
        return events, window if degraded else set(), ""

    events: list = []
    unread: set[date] = set()
    week = _monday(start)
    try:
        with open_session(
            cfg.insite_host, profile=Path(cfg.profile_dir), item=cfg.op_item, vault=cfg.op_vault
        ) as (_ctx, page):
            while week <= end:
                week_events, week_degraded = read_week(
                    page,
                    week,
                    answer_sign_in=lambda p: sign_in(p, credentials(cfg.op_item, cfg.op_vault)),
                )
                events.extend(week_events)
                if week_degraded:
                    unread |= {d for d in window if _monday(d) == week}
                week += timedelta(days=7)
    except Exception as exc:  # noqa: BLE001 - unread days, not a crash
        # Stops at the first failure: reading on would mean signing in again.
        logger.exception("classify: calendar read failed")
        return events, unread | {d for d in window if d >= week}, _failure(exc)
    return events, unread, ""


def _portal(cfg: Config, stack: contextlib.ExitStack, command: str, outcome: str):
    """The signed-in page, or None once a failed sign-in has been reported."""
    try:
        _ctx, page = stack.enter_context(
            open_session(
                cfg.insite_host, profile=Path(cfg.profile_dir), item=cfg.op_item, vault=cfg.op_vault
            )
        )
    except Exception as exc:  # noqa: BLE001 - nothing in AFAS was touched yet
        logger.exception("%s: could not sign in to AFAS", command)
        alert(cfg, f"`{command}` could not sign in to AFAS, {outcome}: {_failure(exc)}")
        return None
    return page


def _each(cfg: Config, step, conn, page, row: dict) -> bool:
    """One declaration. A failure stays with it, so the next one still gets its turn.

    Safe because every step reads AFAS before it clicks, and writes `in_flight`
    before the click itself.
    """
    try:
        return step(cfg, conn, page, row)
    except Exception as exc:  # noqa: BLE001
        label = f"{row['claim_type']} {row['period_no']:02d}/{row['period_year']}"
        logger.exception("submit: %s", label)
        alert(
            cfg,
            f"{label}: stopped by {_failure(exc)}. The ledger keeps its state, and the next "
            "run reads AFAS before it does anything.",
        )
        return False


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
            events, unread, problem = _read_calendar(cfg, start, end)

            if unread:
                # Better to record nothing than to record "no bookings".
                why = f" ({problem})" if problem else ""
                alert(
                    cfg,
                    f"calendar unreadable for {min(unread):%d-%m}..{max(unread):%d-%m}{why}. "
                    "Decided days stay as they are, new days are left undecided.",
                )
            elif problem:
                logger.warning("classify: every day was read, then: %s", problem)

            day = start
            counts: dict[str, int] = {}
            while day <= end:
                prior = existing.get(day)
                degraded = day in unread
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
        alert(cfg, f"no days recorded for the week of {start:%d-%m-%Y}.")
        return 0
    _slack(cfg).chat_postMessage(
        channel=cfg.slack_channel,
        text=f"Weekly review {start:%d-%m-%Y}",
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
    top_up = store.open_round(conn, year, period, claim_type) > 1
    what = f"{claim_type.value} {period:02d}/{year}" + (" (top-up)" if top_up else "")
    _slack(cfg).chat_postMessage(
        channel=cfg.slack_channel,
        text=f"Claim {what} ready",
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
            top_up=top_up,
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
                    f"{period:02d}/{year}: nothing to claim, these days are still undecided: "
                    f"{_dates(unresolved)}.",
                )
            continue

        for claim_type, days in planned.items():
            if not days:
                continue
            existing = store.get_submission(conn, year, period, claim_type)
            if existing:
                state = existing["state"]
                if state in _UNSETTLED_STATES:
                    continue
                same = existing["lines_digest"] == store.lines_digest(days, claim_type)
                # `drafted` is a dry run's rehearsal. A live run still has to do it for real.
                if same and (state in _WAITING_STATES or (state == "drafted" and cfg.dry_run)):
                    continue
            # Past a `submitted` round, days still claimable came after it went in: a top-up.
            work.append((year, period, claim_type, days))
    return work


def _fill_rows(cfg: Config, page, path: str, days: list[date]) -> None:
    page.goto(f"https://{cfg.insite_host}{path}", wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(5_000)
    for day in days:
        window = add_row(page)
        fill_row(page, window, ClaimLine(day))
        confirm_row(page, window)


def _own_draft(conn, claim_type: ClaimType, drafts: list[date]) -> dict | None:
    """The submission behind an open draft, if this app made it and it is on its way.

    Matched on the digest: a draft someone added a line to, or took one from, is no
    longer the one on the card, and needs a human.
    """
    periods = {store.period_for(d) for d in drafts}
    if len(periods) != 1:
        return None
    sub = store.get_submission(conn, *periods.pop(), claim_type)
    if not sub or sub["state"] not in ("awaiting_approval", "approved"):
        return None
    return sub if sub["lines_digest"] == store.lines_digest(drafts, claim_type) else None


def _waits_for(label: str, claim_type: ClaimType, sub: dict) -> str:
    ahead = f"{claim_type.value} {sub['period_no']:02d}/{sub['period_year']}"
    if sub["state"] == "approved":
        return (
            f"{label} waits for {ahead}, which is approved. The next submit sends it in "
            f"and the build after that creates {label}."
        )
    return (
        f"{label} waits for {ahead}, which is waiting for your approval. Once approved, "
        f"the next submit sends it in and the build after that creates {label}."
    )


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
            f"{label}: {_dates(pos.conflict)} are in AFAS with another type or an "
            "unknown status. Left out, check AFAS and correct the day.",
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
                f"{label}: the draft in AFAS does not match the ledger. Draft: "
                f"{_dates(pos.draft + this_period)}, missing: {_dates(pos.absent)}. "
                "Delete or fix the draft in AFAS."
            )
            store.upsert_submission(
                conn, year, period, claim_type, digest, len(todo), "failed", error=problem
            )
            alert(cfg, problem)
            return False
        if pos.other_drafts:
            # The task list cannot tell two drafts of one type apart, so a second
            # would block both. This one waits its turn.
            ahead = _own_draft(conn, claim_type, pos.other_drafts)
            if ahead:
                # The queue working as designed: the card already asks for the one
                # thing a human can do, so this is news, not an alert.
                _alert_once(
                    cfg,
                    conn,
                    f"waits:{claim_type.value}:{_dates(pos.other_drafts)}",
                    _waits_for(label, claim_type, ahead),
                    post=notify,
                )
                return not pos.conflict
            _alert_once(
                cfg,
                conn,
                f"blocked:{claim_type.value}:{_dates(pos.other_drafts)}",
                f"{label} waits: there is already an open {claim_type.value} draft in AFAS "
                f"({_dates(pos.other_drafts)}). Submit or delete it.",
            )
            return False
        if pos.absent:
            _fill_rows(cfg, page, path, pos.absent)
            create_draft(page, dry_run=False)
            after = position(read_lines(page, cfg.insite_host, cfg.claims_page), claim_type, todo)
            if after.draft != todo:
                problem = (
                    f"{label}: clicked Aanmaken, but AFAS shows "
                    f"{_dates(after.draft) or 'nothing'} as the draft instead of {_dates(todo)}."
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
                    f"{due[1]:02d}/{due[0]} is due and the ledger holds no days for it. "
                    "Is classify still running?",
                )

        work = _plan_build(cfg, conn, periods)
        if not work:
            logger.info("build: nothing to do")
            return 0

        ok = True
        with store.browser_lock(conn) as got, contextlib.ExitStack() as stack:
            if not got:
                return _lock_held(cfg, "build")
            page = _portal(cfg, stack, "build", "nothing was built and the next run tries again")
            if page is None:
                return 1
            for year, period, claim_type, days in work:
                try:
                    ok = _build_one(cfg, conn, page, year, period, claim_type, days) and ok
                except Exception as exc:  # noqa: BLE001
                    # One broken form must not cost the other claim type its month.
                    logger.exception("build: %s %02d/%d", claim_type.value, period, year)
                    alert(
                        cfg,
                        f"`build` {claim_type.value} {period:02d}/{year} failed: {_failure(exc)}",
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
        f":white_check_mark: AFAS confirms {claim_type.value} {period:02d}/{year}: "
        f"{len(days)} line(s) are with the manager: {_dates(days)}.",
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
        alert(cfg, f"{label}: the submit did not land, it is still a draft in AFAS.")
        return False
    if days and pos.absent == sorted(days):
        store.set_submission_state(
            conn, year, period, claim_type, "failed", error="draft gone from AFAS"
        )
        alert(cfg, f"{label}: the draft is gone from AFAS. The next build creates it again.")
        return False
    # A rejection lands here too. The next build reads AFAS again and clicks nothing
    # without a new approval, so `failed` is safe and this stops repeating every day.
    problem = (
        f"{label}: unclear state, check AFAS. Submitted: {_dates(pos.filed)}, "
        f"draft: {_dates(pos.draft)}, missing: {_dates(pos.absent)}, "
        f"other: {_dates(pos.conflict)}."
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
        f"{claim_type.value} {period:02d}/{year}: refused, the days changed after the approval.",
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
            f"{label}: the draft in AFAS does not match what was approved. "
            f"Draft: {_dates(pos.draft)}, submitted: {_dates(pos.filed)}, "
            f"missing: {_dates(pos.absent)}, other: {_dates(pos.conflict)}. "
            "The next build posts a new card."
        )
        store.set_submission_state(conn, year, period, claim_type, "failed", error=problem)
        alert(cfg, problem)
        return False
    if pos.other_drafts:
        # The approved draft is intact, so the approval stands and tomorrow's run looks again.
        problem = (
            f"{label}: there is another open draft in AFAS ({_dates(pos.other_drafts)}) and "
            "the task list cannot tell them apart. Submit or delete it."
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
        alert(cfg, f"{label}: submit interrupted, check AFAS: {str(exc)[:300]}")
        return False

    if after.filed != sorted(days):
        problem = (
            f"{label}: clicked submit, but AFAS does not confirm it. Submitted: "
            f"{_dates(after.filed)}, draft: {_dates(after.draft)}, "
            f"missing: {_dates(after.absent)}."
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
                    f":test_tube: DRY RUN: {claim_type.value} {period:02d}/{year} approved, "
                    "nothing was submitted.",
                )
            rows = []

        ready = [row for row in rows if _still_as_approved(cfg, conn, row)]
        ok = len(ready) == len(rows)
        rows = ready

        if not rows and not unsettled:
            logger.info("submit: nothing to file")
            return 0 if ok else 1

        with store.browser_lock(conn) as got, contextlib.ExitStack() as stack:
            if not got:
                return _lock_held(cfg, "submit")
            page = _portal(
                cfg, stack, "submit", "nothing was submitted and approvals stand for the next run"
            )
            if page is None:
                return 1
            for row in unsettled:
                ok = _each(cfg, _settle, conn, page, row) and ok
            for row in rows:
                ok = _each(cfg, _file, conn, page, row) and ok
    return 0 if ok else 1


def _terminated(cfg: Config, command: str) -> None:
    """A deadline or a drain kills the pod with no exception to catch."""
    alert(cfg, f"`{command}` was stopped (deadline or node gone). Check that AFAS is right.")
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
        alert(cfg, f"`{args.command}` crashed: {type(exc).__name__}: {str(exc)[:300]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
