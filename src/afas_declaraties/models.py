"""Core domain types.

Deliberately small and free of I/O so the classification rules can be tested
without a browser, a database or a Slack workspace.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date


class ClaimType(enum.StrEnum):
    """What is being claimed for a day.

    These are mutually exclusive for a given date. Art. 31a lid 12 Wet LB 1964
    bars claiming the commute exemption and the working-from-home exemption on
    the same day, and the database enforces it as well as the code.
    """

    COMMUTE = "commute"
    HOME = "home"


class DayState(enum.StrEnum):
    """Where a classified day is in its lifecycle."""

    PLANNED = "planned"  # classified, not yet confirmed
    NEEDS_INPUT = "needs_input"  # evidence missing or contradictory; ask a human
    CONFIRMED = "confirmed"  # a human said yes, or evidence was unambiguous
    EXCLUDED = "excluded"  # deliberately not claimed (leave, holiday, override)
    DRAFTED = "drafted"  # present on an InSite draft (post-Aanmaken)
    SUBMITTED = "submitted"  # Declaratie insturen done; irreversible
    FAILED = "failed"  # a run errored on this day; needs a look


class Verdict(enum.StrEnum):
    """The classifier's opinion about a single day."""

    OFFICE = "office"
    HOME = "home"
    ABSENT = "absent"  # leave, sick, public holiday: claim nothing
    AMBIGUOUS = "ambiguous"


#: Reason codes. A verdict without one is undebuggable three weeks later, which
#: is exactly when someone asks why a given day was or was not claimed.
class Reason(enum.StrEnum):
    BOOKING_PRESENT = "booking_present"
    NO_BOOKING = "no_booking"
    WEEKEND = "weekend"
    PUBLIC_HOLIDAY = "public_holiday"
    LEAVE_IN_CALENDAR = "leave_in_calendar"
    LEAVE_IN_AFAS = "leave_in_afas"
    HUMAN_OVERRIDE = "human_override"
    BOOKING_WITHDRAWN = "booking_withdrawn"  # was frozen office, booking now gone
    CALENDAR_DEGRADED = "calendar_degraded"  # feed unreachable or suspect


@dataclass(frozen=True)
class CalendarEvent:
    """One event as read from the calendar, normalised."""

    day: date
    subject: str
    all_day: bool
    show_as: str  # busy | free | tentative | oof
    organiser: str = ""

    def is_desk_booking(self, organisers: tuple[str, ...], prefixes: tuple[str, ...]) -> bool:
        """Three independent signals must agree before this counts as an office day.

        Matching on the subject alone is too weak: a meeting titled "Booking
        review" would read as a desk booking. Requiring the organiser and the
        all-day flag as well means a false positive needs three coincidences.
        """
        if not self.all_day:
            return False
        if organisers and not any(o.lower() in self.organiser.lower() for o in organisers):
            return False
        subject = self.subject.lower()
        return any(subject.startswith(p.lower()) for p in prefixes)

    @property
    def is_out_of_office(self) -> bool:
        return self.show_as.lower() == "oof"


@dataclass
class DayClassification:
    """The classifier's output for one date, with its evidence attached."""

    day: date
    verdict: Verdict
    reasons: list[Reason] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    @property
    def claim_type(self) -> ClaimType | None:
        if self.verdict is Verdict.OFFICE:
            return ClaimType.COMMUTE
        if self.verdict is Verdict.HOME:
            return ClaimType.HOME
        return None

    @property
    def state(self) -> DayState:
        if self.verdict is Verdict.AMBIGUOUS:
            return DayState.NEEDS_INPUT
        if self.verdict is Verdict.ABSENT:
            return DayState.EXCLUDED
        return DayState.PLANNED
