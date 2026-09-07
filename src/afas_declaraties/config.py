"""Runtime configuration, read once from the environment and validated.

Nothing here has a tenant-specific default. Every identifying value -- the
portal hostname, the 1Password vault, the Slack channel, the approver list --
arrives from the environment, because this repository is public.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date


class ConfigError(RuntimeError):
    """The environment is not fit to run against."""


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


@dataclass(frozen=True)
class Config:
    database_url: str
    insite_host: str
    op_item: str
    op_vault: str
    slack_bot_token: str
    slack_app_token: str
    slack_channel: str
    approver_ids: frozenset[str]
    dry_run: bool
    profile_dir: str
    booking_organisers: tuple[str, ...]
    booking_prefixes: tuple[str, ...]
    excluded_dates: frozenset[date] = field(default_factory=frozenset)

    #: Where the calendar is read from. ``owa`` drives Outlook Web with
    #: Playwright; ``mcp`` reads it over the Microsoft 365 MCP connector and
    #: needs no browser at all. There is deliberately no automatic fallback
    #: between them: silently swapping the source a claim is derived from is
    #: how "the read failed" turns into "there were no office days".
    calendar_source: str = "owa"
    #: The zone a calendar event's local date is decided in.
    timezone: str = "Europe/Amsterdam"
    #: Treat a working window with no calendar entries at all as a question
    #: rather than as evidence. See :func:`calendar_mcp.read_range`.
    require_calendar_events: bool = True

    #: Page slugs, configurable because they are publication names chosen by
    #: the employer and differ per environment.
    commute_page: str = ""
    home_page: str = ""

    @classmethod
    def from_env(cls) -> Config:
        dry_run = _bool("DRY_RUN", True)
        approvers = frozenset(
            # Slack ids are case-sensitive; never normalise them.
            p.strip()
            for p in os.environ.get("SLACK_APPROVER_IDS", "").split(",")
            if p.strip()
        )
        # An empty allowlist means "anybody who can see the channel may spend
        # money". That is a misconfiguration, not a permissive default, so it
        # is refused rather than silently accepted.
        if not dry_run and not approvers:
            raise ConfigError(
                "SLACK_APPROVER_IDS is empty while DRY_RUN=false: refusing to start, "
                "because any channel member could approve a real submission"
            )

        excluded = frozenset(
            date.fromisoformat(d.strip())
            for d in os.environ.get("EXCLUDED_DATES", "").split(",")
            if d.strip()
        )

        # An unrecognised source is refused rather than defaulted. Quietly
        # falling back to the browser because CALENDAR_SOURCE was misspelt
        # would look like the setting had been ignored, which is exactly the
        # kind of thing nobody notices until a month of claims is wrong.
        source = os.environ.get("CALENDAR_SOURCE", "owa").strip().lower()
        if source not in {"owa", "mcp"}:
            raise ConfigError(f"CALENDAR_SOURCE must be 'owa' or 'mcp', not {source!r}")

        return cls(
            database_url=_require("DATABASE_URL"),
            insite_host=_require("INSITE_HOST"),
            op_item=os.environ.get("OP_ITEM_NAME", "Microsoft"),
            op_vault=os.environ.get("OP_VAULT", "Private"),
            slack_bot_token=os.environ.get("SLACK_BOT_TOKEN", ""),
            slack_app_token=os.environ.get("SLACK_APP_TOKEN", ""),
            slack_channel=os.environ.get("SLACK_CHANNEL_ID", ""),
            approver_ids=approvers,
            dry_run=dry_run,
            profile_dir=os.environ.get("BROWSER_PROFILE_DIR", "/tmp/browser-profile"),
            booking_organisers=tuple(
                s.strip()
                for s in os.environ.get("BOOKING_ORGANISERS", "deskbird").split(",")
                if s.strip()
            ),
            booking_prefixes=tuple(
                s.strip()
                for s in os.environ.get("BOOKING_SUBJECT_PREFIXES", "Booking").split(",")
                if s.strip()
            ),
            excluded_dates=excluded,
            commute_page=_require("COMMUTE_PAGE_PATH"),
            home_page=_require("HOME_PAGE_PATH"),
            calendar_source=source,
            timezone=os.environ.get("TZ", "Europe/Amsterdam"),
            require_calendar_events=_bool("REQUIRE_CALENDAR_EVENTS", True),
        )

    def require_slack(self) -> None:
        if not self.slack_bot_token or not self.slack_app_token:
            raise ConfigError("SLACK_BOT_TOKEN and SLACK_APP_TOKEN are required for Slack features")
        if not self.slack_channel:
            raise ConfigError("SLACK_CHANNEL_ID is required")
