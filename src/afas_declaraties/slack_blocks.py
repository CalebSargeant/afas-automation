"""Block Kit payloads.

Kept free of I/O so the message and modal structure can be asserted in tests
without a workspace, and so the approval encoding has one definition.
"""

from __future__ import annotations

from datetime import date

from .models import ClaimType

#: Separator for button values. Neither an ISO date, a period id nor a hex
#: digest can contain it, so parsing is unambiguous and a malformed value is a
#: bug or a forged click rather than something to coerce into a decision.
SEP = "|"

APPROVE = "period_approve"
REJECT = "period_reject"
CORRECT = "open_corrections"
MODAL_CALLBACK = "day_corrections"

_CHOICES = [
    ("office", "Kantoor (woon-werk)"),
    ("home", "Thuis"),
    ("absent", "Verlof / niet gewerkt"),
]


def decision_value(year: int, period: int, claim_type: ClaimType, digest: str) -> str:
    return SEP.join([str(year), str(period), claim_type.value, digest])


def parse_decision_value(raw: str) -> tuple[int, int, ClaimType, str]:
    """Strict arity. A value that does not have exactly four parts is refused."""
    parts = raw.split(SEP)
    if len(parts) != 4:
        raise ValueError(f"malformed decision value: {raw!r}")
    year, period, claim, digest = parts
    return int(year), int(period), ClaimType(claim), digest


def approval_card(
    *,
    year: int,
    period: int,
    claim_type: ClaimType,
    days: list[date],
    digest: str,
    unresolved: list[date],
    dry_run: bool,
    already: list[date] | None = None,
    unrecorded: list[date] | None = None,
    open_drafts: list[date] | None = None,
) -> list[dict]:
    """The monthly ask. Everything that will be filed is shown before approval."""
    label = "woon-werk" if claim_type is ClaimType.COMMUTE else "thuiswerkdagen"
    day_lines = "\n".join(f"• {d.strftime('%a %d-%m-%Y')}" for d in days) or "_geen dagen_"

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Declaratie {label} — {period:02d}/{year}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Dagen*\n{len(days)}"},
                {"type": "mrkdwn", "text": f"*Periode*\n{period:02d}/{year}"},
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": day_lines}},
    ]

    if unresolved:
        # Named explicitly: a day dropped for lack of an answer must be visible
        # at the moment of approval, not discovered on the payslip.
        missing = ", ".join(d.strftime("%d-%m") for d in unresolved)
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":warning: *Niet meegenomen* ({len(unresolved)}): {missing}\n"
                    "_Deze dagen zijn onbeslist gebleven en worden niet gedeclareerd._",
                },
            }
        )

    for icon, title, listed, note in (
        (":warning:", "Nooit geclassificeerd", unrecorded, "Worden niet gedeclareerd."),
        (":white_check_mark:", "Al in AFAS", already, "Niet nog een keer op deze declaratie."),
        (":warning:", "Open concept in AFAS", open_drafts, "Blokkeert insturen van dit type."),
    ):
        if listed:
            names = ", ".join(d.strftime("%d-%m") for d in listed)
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{icon} *{title}* ({len(listed)}): {names}\n_{note}_",
                    },
                }
            )

    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"digest `{digest}` · {'DRY RUN — er wordt niets ingediend' if dry_run else 'Goedkeuren dient de declaratie definitief in'}",
                }
            ],
        }
    )

    value = decision_value(year, period, claim_type, digest)
    blocks.append(
        {
            "type": "actions",
            "block_id": "period_decision",
            "elements": [
                {
                    "type": "button",
                    "action_id": APPROVE,
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Goedkeuren en insturen"},
                    "value": value,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Definitief insturen?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{len(days)}* dagen {label} voor periode {period:02d}/{year}. "
                            "Dit is onomkeerbaar en gaat naar je leidinggevende.",
                        },
                        "confirm": {"type": "plain_text", "text": "Insturen"},
                        "deny": {"type": "plain_text", "text": "Annuleren"},
                    },
                },
                {
                    "type": "button",
                    "action_id": REJECT,
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Afwijzen"},
                    "value": value,
                },
                {
                    "type": "button",
                    "action_id": CORRECT,
                    "text": {"type": "plain_text", "text": "Dagen corrigeren"},
                    "value": value,
                },
            ],
        }
    )
    return blocks


def weekly_digest(rows: list[dict], *, week_start: date) -> list[dict]:
    """The weekly review. Unresolved days lead, because they need an answer."""
    needs = [r for r in rows if r["state"] == "needs_input"]
    settled = [r for r in rows if r["state"] != "needs_input"]

    icon = {"commute": ":office:", "home": ":house:", None: ":palm_tree:"}
    lines = []
    for r in settled:
        lines.append(
            f"{icon.get(r['claim_type'], ':grey_question:')} "
            f"{r['day'].strftime('%a %d-%m')} — {r['claim_type'] or 'niets'}"
        )

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Week van {week_start.strftime('%d-%m-%Y')}"},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines) or "_geen dagen_"}},
    ]

    if needs:
        detail = "\n".join(
            f"• *{r['day'].strftime('%a %d-%m')}* — {', '.join(r.get('reasons') or []) or 'onbekend'}"
            for r in needs
        )
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":question: *Onbeslist — deze vervallen als je niets doet*\n{detail}",
                },
            }
        )

    blocks.append(
        {
            "type": "actions",
            "block_id": "week_actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": CORRECT,
                    "text": {"type": "plain_text", "text": "Dagen corrigeren"},
                    "value": f"week{SEP}{week_start.isoformat()}",
                }
            ],
        }
    )
    return blocks


def corrections_modal(days: list[dict], *, private_metadata: str) -> dict:
    """One radio group per day. Leaving a day untouched changes nothing."""
    blocks = []
    for r in days:
        d: date = r["day"]
        current = r.get("claim_type")
        initial = {"commute": "office", "home": "home", None: "absent"}.get(current)
        options = [
            {"text": {"type": "plain_text", "text": text}, "value": val} for val, text in _CHOICES
        ]
        element = {"type": "radio_buttons", "action_id": "choice", "options": options}
        if initial:
            element["initial_option"] = next(o for o in options if o["value"] == initial)
        blocks.append(
            {
                "type": "input",
                "block_id": f"day{SEP}{d.isoformat()}",
                "optional": True,
                "label": {"type": "plain_text", "text": d.strftime("%A %d-%m-%Y")},
                "element": element,
            }
        )

    return {
        "type": "modal",
        "callback_id": MODAL_CALLBACK,
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "Dagen corrigeren"},
        "submit": {"type": "plain_text", "text": "Opslaan"},
        "close": {"type": "plain_text", "text": "Annuleren"},
        "blocks": blocks
        or [{"type": "section", "text": {"type": "mrkdwn", "text": "_Niets te corrigeren._"}}],
    }
