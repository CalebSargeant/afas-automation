"""The claim ledger, on the shared CNPG Postgres.

This is the system of record, not AFAS. AFAS is an output format: a portal
change, a rejected line or a lost session must never lose the knowledge of what
was worked where. Raw SQL with idempotent DDL, matching the house pattern in
github-timesheet -- no ORM, no migration tool.

The central invariant is structural rather than procedural: ``claim_day`` is
keyed by date, so a single day physically cannot hold both a commute and a
working-from-home claim. Art. 31a lid 12 Wet LB 1964 makes those two mutually
exclusive per day, and an invariant that matters this much should be enforced
by the database rather than by remembering to check.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date

import psycopg
from psycopg.rows import dict_row

from .models import ClaimType, DayClassification, DayState, Verdict

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_day (
    day             date PRIMARY KEY,
    claim_type      text,
    state           text        NOT NULL,
    verdict         text        NOT NULL,
    reasons         text[]      NOT NULL DEFAULT '{}',
    evidence        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    period_year     integer,
    period_no       integer,
    overridden_by   text,
    overridden_at   timestamptz,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT claim_type_valid
        CHECK (claim_type IS NULL OR claim_type IN ('commute', 'home')),
    -- A day that will be claimed must say what it is claiming. A day that will
    -- not be claimed must not carry a claim type it could later be read as.
    CONSTRAINT state_matches_claim_type CHECK (
        (state IN ('planned','confirmed','drafted','submitted') AND claim_type IS NOT NULL)
     OR (state IN ('needs_input','excluded','failed'))
    )
);

CREATE INDEX IF NOT EXISTS claim_day_period_idx ON claim_day (period_year, period_no);
CREATE INDEX IF NOT EXISTS claim_day_state_idx  ON claim_day (state);

CREATE TABLE IF NOT EXISTS submission (
    id            bigserial PRIMARY KEY,
    period_year   integer NOT NULL,
    period_no     integer NOT NULL,
    claim_type    text    NOT NULL,
    lines_digest  text    NOT NULL,
    line_count    integer NOT NULL,
    state         text    NOT NULL,
    approved_by   text,
    approved_at   timestamptz,
    insite_ref    text,
    last_error    text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT submission_state_valid CHECK (
        state IN ('drafted','awaiting_approval','approved','in_flight',
                  'submitted','rejected','failed','needs_reconciliation')
    ),
    CONSTRAINT one_submission_per_period UNIQUE (period_year, period_no, claim_type)
);

-- Socket Mode redelivers on reconnect, so every handler must be able to
-- recognise a replay. Without this a reconnect during an approval could submit
-- the same declaration twice.
CREATE TABLE IF NOT EXISTS slack_interaction (
    dedupe_key  text PRIMARY KEY,
    kind        text NOT NULL,
    actor       text,
    handled_at  timestamptz NOT NULL DEFAULT now()
);
"""

#: Namespaced advisory-lock id. Browser jobs must not overlap: two concurrent
#: headless logins against one corporate account is how an account gets locked.
BROWSER_LOCK_ID = 0x_AFA5_DEC1


@contextmanager
def connect(dsn: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(dsn, row_factory=dict_row, autocommit=False) as conn:
        yield conn


def ensure_schema(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
    conn.commit()
    logger.info("store: schema ensured")


@contextmanager
def browser_lock(conn: psycopg.Connection) -> Iterator[bool]:
    """Session-scoped advisory lock around any browser-driving work.

    Yields False rather than blocking, so a job that cannot get the lock exits
    cleanly instead of queueing behind one that may be stuck in an SSO flow.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s) AS got", (BROWSER_LOCK_ID,))
        got = cur.fetchone()["got"]
    try:
        yield got
    finally:
        if got:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (BROWSER_LOCK_ID,))


def period_for(day: date) -> tuple[int, int]:
    """AFAS payroll period for a date.

    Calendar months, which matches the environment observed. Kept as one
    function so a 13-period calendar becomes a single change rather than a hunt
    through every caller.
    """
    return day.year, day.month


def record_day(conn: psycopg.Connection, c: DayClassification) -> None:
    """Freeze one classified day.

    A day already submitted is never rewritten: the claim is filed and the
    ledger must keep saying what was filed, whatever the calendar says later.
    A human override is likewise not overwritten by a subsequent nightly run.
    """
    year, period = period_for(c.day)
    claim_type = c.claim_type.value if c.claim_type else None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO claim_day (day, claim_type, state, verdict, reasons,
                                   evidence, period_year, period_no)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (day) DO UPDATE SET
                claim_type = EXCLUDED.claim_type,
                state      = EXCLUDED.state,
                verdict    = EXCLUDED.verdict,
                reasons    = EXCLUDED.reasons,
                evidence   = EXCLUDED.evidence,
                period_year= EXCLUDED.period_year,
                period_no  = EXCLUDED.period_no,
                updated_at = now()
            WHERE claim_day.state NOT IN ('submitted', 'drafted')
              AND claim_day.overridden_by IS NULL
            """,
            (
                c.day,
                claim_type,
                c.state.value,
                c.verdict.value,
                [r.value for r in c.reasons],
                json.dumps(c.evidence),
                year,
                period,
            ),
        )
    conn.commit()


#: What a human decision means as a verdict.
#:
#: The verdict is REPLACED, not left alone. A row reading
#: `verdict=office, claim_type=home` claims one thing and reads as another, and
#: the whole point of the ledger is to still make sense to someone reading it
#: three weeks later. That a person decided it is carried by the
#: `human_override` reason, which is what reason codes are for.
_OVERRIDE_VERDICT: dict[ClaimType | None, Verdict] = {
    ClaimType.COMMUTE: Verdict.OFFICE,
    ClaimType.HOME: Verdict.HOME,
    None: Verdict.ABSENT,
}


def apply_override(
    conn: psycopg.Connection, day: date, claim_type: ClaimType | None, actor: str
) -> bool:
    """Record a human decision for a day. Returns False if the day is already filed."""
    state = DayState.CONFIRMED.value if claim_type else DayState.EXCLUDED.value
    verdict = _OVERRIDE_VERDICT[claim_type].value
    year, period = period_for(day)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO claim_day (day, claim_type, state, verdict, reasons,
                                   evidence, period_year, period_no,
                                   overridden_by, overridden_at)
            VALUES (%s, %s, %s, %s, ARRAY['human_override'], '{}'::jsonb, %s, %s, %s, now())
            ON CONFLICT (day) DO UPDATE SET
                claim_type    = EXCLUDED.claim_type,
                state         = EXCLUDED.state,
                verdict       = EXCLUDED.verdict,
                reasons       = claim_day.reasons || ARRAY['human_override'],
                overridden_by = EXCLUDED.overridden_by,
                overridden_at = now(),
                updated_at    = now()
            WHERE claim_day.state <> 'submitted'
            RETURNING day
            """,
            (day, claim_type.value if claim_type else None, state, verdict, year, period, actor),
        )
        changed = cur.fetchone() is not None
    conn.commit()
    if not changed:
        logger.warning("store: refusing to override %s, already submitted", day)
    return changed


def claimable_days(
    conn: psycopg.Connection, year: int, period: int, claim_type: ClaimType
) -> list[dict]:
    """Days ready to be filed for a period. Excludes anything unresolved."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT day, claim_type, state, reasons
            FROM claim_day
            WHERE period_year = %s AND period_no = %s
              AND claim_type = %s
              AND state IN ('planned', 'confirmed')
            ORDER BY day
            """,
            (year, period, claim_type.value),
        )
        return cur.fetchall()


def unresolved_days(conn: psycopg.Connection, year: int, period: int) -> list[dict]:
    """Days that need a human before the period can be considered complete."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT day, verdict, reasons FROM claim_day
            WHERE period_year = %s AND period_no = %s AND state = 'needs_input'
            ORDER BY day
            """,
            (year, period),
        )
        return cur.fetchall()


def lines_digest(days: Sequence[date], claim_type: ClaimType) -> str:
    """Stable fingerprint of exactly what would be filed.

    A Slack approval is bound to this. If the set of days changes after the
    user approves, the digest moves and the approval is void -- so an approval
    can never be replayed against different content.
    """
    payload = claim_type.value + "|" + ",".join(d.isoformat() for d in sorted(days))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def claim_slack_interaction(
    conn: psycopg.Connection, dedupe_key: str, kind: str, actor: str
) -> bool:
    """True if this interaction is new. False means Slack redelivered it."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO slack_interaction (dedupe_key, kind, actor)
               VALUES (%s, %s, %s) ON CONFLICT (dedupe_key) DO NOTHING
               RETURNING dedupe_key""",
            (dedupe_key, kind, actor),
        )
        fresh = cur.fetchone() is not None
    conn.commit()
    return fresh


def upsert_submission(
    conn: psycopg.Connection,
    year: int,
    period: int,
    claim_type: ClaimType,
    digest: str,
    line_count: int,
    state: str,
) -> None:
    """Record (or refresh) the declaration we intend to file for a period."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO submission (period_year, period_no, claim_type,
                                    lines_digest, line_count, state)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (period_year, period_no, claim_type) DO UPDATE SET
                lines_digest = EXCLUDED.lines_digest,
                line_count   = EXCLUDED.line_count,
                state        = EXCLUDED.state,
                updated_at   = now()
            -- A filed declaration is history. Never rewrite it, even if the
            -- classifier later changes its mind about one of the days.
            WHERE submission.state NOT IN ('submitted', 'in_flight')
            """,
            (year, period, claim_type.value, digest, line_count, state),
        )
    conn.commit()


def get_submission(
    conn: psycopg.Connection, year: int, period: int, claim_type: ClaimType
) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM submission
               WHERE period_year=%s AND period_no=%s AND claim_type=%s""",
            (year, period, claim_type.value),
        )
        return cur.fetchone()


def approved_submissions(conn: psycopg.Connection) -> list[dict]:
    """Every period a human has approved and that has not been filed yet.

    Selected by state, never by date. `build` runs on the 28th and defaults to
    the *previous* month, so a submit that woke on the 1st and worked out
    "previous month" for itself would name the month after the one that was
    approved, and file the wrong period into a portal with no undo. The
    approval already names its period; this reads it back.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM submission
               WHERE state = 'approved'
               ORDER BY period_year, period_no, claim_type"""
        )
        return cur.fetchall()


def approve_submission(
    conn: psycopg.Connection,
    year: int,
    period: int,
    claim_type: ClaimType,
    digest: str,
    actor: str,
) -> tuple[bool, str]:
    """Approve a period, but only if it still matches what was approved.

    The digest is re-derived from the ledger at approval time and compared with
    the one carried by the button. If days were added or removed since the card
    was posted, the approval is for different content and is refused -- an
    approval must never be replayable against a changed claim.
    """
    current = claimable_days(conn, year, period, claim_type)
    live_digest = lines_digest([r["day"] for r in current], claim_type)
    if live_digest != digest:
        return False, (
            f"the claim changed since this card was posted "
            f"(approved {digest}, now {live_digest}); a fresh approval is needed"
        )

    with conn.cursor() as cur:
        cur.execute(
            """UPDATE submission SET state='approved', approved_by=%s, approved_at=now(),
                                     updated_at=now()
               WHERE period_year=%s AND period_no=%s AND claim_type=%s
                 AND state IN ('drafted','awaiting_approval','rejected')
               RETURNING id""",
            (actor, year, period, claim_type.value),
        )
        ok = cur.fetchone() is not None
    conn.commit()
    if not ok:
        return False, "this period is not awaiting approval (already submitted, or in flight)"
    return True, f"approved {len(current)} day(s)"


def set_submission_state(
    conn: psycopg.Connection,
    year: int,
    period: int,
    claim_type: ClaimType,
    state: str,
    *,
    error: str | None = None,
    insite_ref: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE submission SET state=%s, last_error=%s,
                      insite_ref=COALESCE(%s, insite_ref), updated_at=now()
               WHERE period_year=%s AND period_no=%s AND claim_type=%s""",
            (state, error, insite_ref, year, period, claim_type.value),
        )
    conn.commit()


def days_in_period(conn: psycopg.Connection, year: int, period: int) -> list[dict]:
    """Every recorded day in a period, whatever its state -- for the correction modal."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT day, claim_type, state, reasons FROM claim_day
               WHERE period_year=%s AND period_no=%s ORDER BY day""",
            (year, period),
        )
        return cur.fetchall()


def days_between(conn: psycopg.Connection, start: date, end: date) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT day, claim_type, state, reasons FROM claim_day
               WHERE day >= %s AND day <= %s ORDER BY day""",
            (start, end),
        )
        return cur.fetchall()


def mark_days_state(conn: psycopg.Connection, days: Sequence[date], state: DayState) -> None:
    """Advance a set of days, without disturbing anything already submitted."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE claim_day SET state=%s, updated_at=now()
               WHERE day = ANY(%s) AND state <> 'submitted'""",
            (state.value, list(days)),
        )
    conn.commit()
