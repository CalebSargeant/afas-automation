# Architecture

## Components

| Component | Path | Role |
|---|---|---|
| Domain types | `src/afas_declaraties/models.py` | `ClaimType`, `DayState`, `Verdict`, `Reason`, `CalendarEvent`, `DayClassification`. No I/O. |
| Classifier | `src/afas_declaraties/classify.py` | `classify_day()` and `ClassifierConfig`. No I/O. |
| Calendar reader | `src/afas_declaraties/calendar_owa.py` | Parses Outlook Web `aria-label` strings; `read_week()` drives the page. |
| Calendar reader (MCP) | `src/afas_declaraties/calendar_mcp.py` | The browser-free alternative. `parse_event()` is pure; `read_range()` answers the same `(events, degraded)` contract. |
| MCP transport | `src/afas_declaraties/m365_mcp.py` | Device-code auth against Anthropic's multi-tenant app pair, refresh-token cache, JSON-RPC, paging. Stdlib only. |
| Entra sign-in | `src/afas_declaraties/entra.py` | Interactive Microsoft SSO as a state machine. |
| Session | `src/afas_declaraties/session.py` | `open_session()`: a Chromium context that is signed in and settled. |
| Credentials | `src/afas_declaraties/onepassword.py` | `get_field()` / `get_totp()` via the `op` CLI. |
| Portal driver | `src/afas_declaraties/insite.py` | The verzameldeclaratie form: rows, verification, draft creation. |
| Ledger | `src/afas_declaraties/store.py` | Raw-SQL Postgres store, advisory lock, approval digest, interaction dedupe. |
| Config | `src/afas_declaraties/config.py` | `Config.from_env()`. No tenant-specific defaults. |
| Slack blocks | `src/afas_declaraties/slack_blocks.py` | `weekly_digest()`, `approval_card()`. Pure. |
| Slack daemon | `src/afas_declaraties/slackd.py` | Socket Mode app and the `/tmp/healthy` heartbeat. |
| CLI | `src/afas_declaraties/cli.py` | `classify`, `digest`, `build`, `slackd`, `override`. |
| Recon tooling | `scripts/` | `bootstrap_session.py`, `explore_insite.py`. Dev only. |
| Chart | `charts/afas-declaraties/` | In-repo Helm chart, released with the image. |

## Control flow

### Classify

Runs nightly over a **trailing window** (default 9 days) rather than only
yesterday, so a day whose booking was cancelled after it was first seen gets
reconsidered. That reconciliation is what turns "I deleted the calendar entry"
into a question instead of a silently wrong claim.

`CALENDAR_SOURCE` picks which reader runs. Both answer `(events, degraded)` and
the classifier cannot tell them apart. There is **no fallback between them**:
quietly swapping the source a claim is derived from is how "the read failed"
turns into "there were no office days".

With `CALENDAR_SOURCE=mcp` (no browser):

1. `m365_mcp.token()` refreshes the cached Entra token, or fails immediately
   telling you to run the one-off login. It will not stop and wait for a device
   code, because a CronJob has nobody to type one.
2. `read_range()` pages `outlook_calendar_search` over the window, reaching a
   month further back than it needs so an absence that *started* earlier is
   still seen, then clips back to the window. All-day events expand across every
   day they span; a failed, partial or entirely empty read comes back degraded.

With `CALENDAR_SOURCE=owa` (the default):

1. `open_session()` launches a persistent Chromium context and navigates to the
   portal. If Entra answers, `sign_in()` runs; otherwise the existing session is
   accepted with no credentials entered at all.
2. `wait_until_settled()` blocks until the browser is on the application host
   **and** off any transient callback path.
3. `read_week()` navigates OWA's work-week view, harvests every element with a
   descriptive `aria-label`, and parses each one into a `CalendarEvent`. It
   returns `(events, degraded)`.

Either way:

4. `classify_day()` produces a `DayClassification` per date.
5. `store.record_day()` freezes each one.

The browser advisory lock is taken only when a browser is actually involved. It
exists to stop two corporate SSO sign-ins running at once; the MCP reader
performs none, so the nightly classification no longer queues behind a `build`
stuck in a sign-in flow.

### Build

Defaults to the **previous** month, and runs on the 28th so there is room to fix
a problem before the period closes.

1. `store.unresolved_days()` is logged loudly. Unresolved days are **not**
   claimed, they are reported.
2. Take `store.claimable_days()` for the period and each claim type, and open
   that type's page slug.
3. For each day: `add_row()` (discovering the window index), `fill_row()`,
   `verify_row()`, `confirm_row()`.
4. `create_draft()` clicks the page-level `Aanmaken`, producing a
   *Declaratie voorbereiden* task the employee can still amend or delete. It is a
   no-op under `DRY_RUN`, which is the difference between submission state
   `drafted` and `awaiting_approval`.
5. Record the submission with its `lines_digest`, mark the days `drafted`, and
   post the approval card to Slack.

!!! danger "The final submission is live"
    *Declaratie insturen* on the draft task is the one irreversible step in
    the system, and `cli submit` performs it whenever `DRY_RUN=false`. Three
    gates stand in front of it: `DRY_RUN` defaults to true, an allow-listed
    Slack user must approve, and the approval's `lines_digest` is re-derived
    from the ledger and must still match.

    Task selection matches the form slug, and two open drafts of the same claim
    type are refused rather than guessed -- the task list shows no period, and
    filing the wrong month cannot be undone.

    The click itself has never been executed against the live portal, because
    doing so files a real claim. Everything up to it has been.

### Submit

Runs daily at 07:00 and files whatever a human approved. Most mornings it reads
one row, finds nothing in `approved` and exits before opening a browser.

It takes **no period**. `build` runs on the 28th for the *previous* month, so a
submit that woke on the 1st and worked out "last month" for itself would name
the month after the one that was approved -- and the portal has no undo. The
approved row already carries `period_year` and `period_no`, and those are the
only period this uses. `--year`/`--period` exist for a manual run and must be
given together.

Before each claim type:

1. Re-derive `lines_digest` from the ledger. If it no longer matches what was
   approved, the approval was for different content: the submission drops back
   to `awaiting_approval` and nothing is filed.
2. `in_flight` is written **before** the click, so a process that dies mid-click
   cannot be read as "never submitted" and retried.
3. Under `DRY_RUN` the state returns to `approved`, so the same run repeats
   harmlessly tomorrow.

Without this job the approval click updates Postgres and stops there: the last
step into AFAS stays manual, which is the thing the pipeline exists to remove.

## The classification rules

Evaluated in this order. The order is part of the design, not an accident.

| # | Condition | Verdict | Reason |
|---|---|---|---|
| 1 | Not a configured working weekday | `absent` | `weekend` |
| 2 | Date in `excluded_dates` | `absent` | `public_holiday` |
| 3 | **Calendar read degraded** | `ambiguous` | `calendar_degraded` |
| 4 | On leave in AFAS | `absent` | `leave_in_afas` |
| 5 | An out-of-office event in the calendar | `absent` | `leave_in_calendar` |
| 6 | A desk booking present | `office` | `booking_present` |
| 7 | No booking, but a previous run froze this as office | `ambiguous` | `booking_withdrawn` |
| 8 | No booking | `home` | `no_booking` |

Rule 3 sits above everything that could produce a claim, so a broken calendar
read can never fall through to rule 8.

Rule 6 needs **three independent signals** to agree, checked in
`CalendarEvent.is_desk_booking()`: the event is all-day, the organiser matches a
configured booking organiser, and the subject starts with a configured prefix.
Matching on the subject alone is too weak, a meeting titled "Booking review" would
read as a desk booking, so a false positive needs three coincidences.

Rules 4 and 5 sit above rule 6 deliberately: a booking nobody cancelled must not
produce a commute claim for a day spent on leave.

!!! danger "Rule 3 is the one that matters"
    "No events found" and "the page did not load" look identical downstream.
    Treating the second as the first turns an outage into a month of
    working-from-home claims that were never true. Both readers therefore
    return a `degraded` flag alongside the events, and neither is allowed to
    turn a failure into an empty list. Never discard it.

    `read_week()` sets it when labels were harvested but every one failed to
    parse. `read_range()` sets it when the connector errored, when it said its
    answer was partial, when every event failed to parse -- and, unless
    `REQUIRE_CALENDAR_EVENTS=false`, when the whole window came back with no
    events at all. That last one is deliberate paranoia: a wildcard search that
    quietly stopped matching returns a clean, empty, entirely plausible answer.
    Being asked about a genuinely quiet week costs one Slack click.

!!! note "Rule 7 asks rather than assumes"
    Deleting a calendar entry after the fact is how the user says "I did not
    actually go in". That signal must not be lost, but it must not be acted on
    silently either: the two sources now disagree and only a human knows which
    was true.

Every classification carries `Reason` codes and an `evidence` dict. A verdict
without them is undebuggable three weeks later, which is exactly when someone
asks why a day was or was not claimed.

## The ledger

Raw SQL with idempotent DDL, no ORM and no migration tool, matching the house
pattern in `github-timesheet`.

### `claim_day`

Keyed by `day`. That is the central invariant, expressed structurally: a single
date physically cannot hold both a commute and a working-from-home claim, which
is what art. 31a lid 12 Wet LB 1964 requires. An invariant this important is
enforced by the database rather than by remembering to check.

A `CHECK` constraint also ties state to claim type: a day that will be claimed
must say what it is claiming, and a day that will not be claimed must not carry a
claim type that could later be read as one.

### State machine

```
planned ──> confirmed ──> drafted ──> submitted
   │            ^
   │            │ human override
   ├──> needs_input
   ├──> excluded
   └──> failed
```

Two write rules protect history:

- `record_day()` will not overwrite a day that is `submitted` or `drafted`, or
  one carrying a human override. The claim is filed and the ledger must keep
  saying what was filed, whatever the calendar says afterwards.
- `apply_override()` will not touch a `submitted` day, and returns `False` so the
  caller can say so out loud.

### `submission` and approval binding

`lines_digest()` produces a stable fingerprint of exactly which days would be
filed. A Slack approval is bound to that digest, so if the set of days changes
after the card is posted the digest moves and the approval is void. An approval
can never be replayed against different content.

### `slack_interaction`

Socket Mode redelivers on reconnect. Every handler opens by inserting a dedupe
key; zero rows affected means Slack replayed the event. Without this, a
reconnect during an approval could submit the same declaration twice.

### `browser_lock()`

A namespaced `pg_try_advisory_lock`. It **yields `False` rather than blocking**,
so a job that cannot get the lock exits cleanly instead of queueing behind one
that may be stuck in an SSO flow. Two concurrent headless logins against one
corporate account is how an account gets locked.

## Slack

`slackd` holds a Socket Mode WebSocket. Three properties are structural, not
preferences:

- **Exactly one replica.** Slack load-balances events across an app's sockets and
  delivers each event to exactly one of them, so a second replica gets a random
  subset and silently drops the rest. `strategy: Recreate`, replicas hard-coded
  in the template.
- **Ack first, then work.** Handlers acknowledge inside Slack's three-second
  budget, then write a single row. `slackd` **never drives Playwright**: it keeps
  the WebSocket process small, restartable, and unable to submit a claim on its
  own.
- **Every handler is idempotent.** Socket Mode redelivers on reconnect, so each
  handler opens with an insert into `slack_interaction`; zero rows affected means
  this is a replay.

Approvers come from `SLACK_APPROVER_IDS`, parsed into a frozenset and **never
lowercased**, because Slack ids are case-sensitive. An empty allowlist means any
channel member could approve a real submission, so `Config.from_env()` refuses to
start when `DRY_RUN=false` and the list is empty.

Health is a heartbeat file at `/tmp/healthy`, touched on connect and on every
handled event, checked by an exec probe. There is no HTTP server, no port, no
Service and no Ingress: Socket Mode dials out.

## Portal and identity-provider quirks

These shaped the code more than anything else. The full list, with symptoms and
fixes, is in `.claude/COMMON_MISTAKES.md`; the load-bearing ones are summarised
here.

!!! warning "Entra keeps every sign-in pane in the DOM at once"
    On the password pane the email input is still visible with a non-zero box,
    merely parked in a corner, `aria-hidden` and untabbable. Playwright reports
    both fields as visible on both panes, so `is_visible()` cannot say which step
    is on screen. `entra._is_active()` discriminates with `offsetParent`, an
    `aria-hidden` ancestor check, `tabIndex` and the client rect. Getting this
    wrong re-clicks the shared `#idSIButton9` and submits an empty password.

!!! warning "Two InSite buttons read “Aanmaken”"
    `Window_<n>_Actions_AntaUpdateCloseWebForm` confirms **one row**;
    `Window_0_Actions_AntaUpdateCloseWebForm` files **the whole declaration**.
    They are addressed by id, never by label. `<n>` is a per-page-load counter
    and is discovered, never assumed.

!!! warning "`Periode` prefills to the current month"
    Not the booking date's month, and editing the date does not update it. Filing
    August days in September silently books them into period 9, with no error
    anywhere. Every row sets the period explicitly, after the date, and
    `verify_row()` asserts it before the row is committed.

!!! note "Shadow DOM, twice over"
    Each field id exists on both the custom-element host and the inner shadow
    `<input>`. Date and number values live on the inner input's property;
    `afas-reference` values live on the host's attribute. Reading the wrong one
    returns `None` and looks like an empty form.

!!! note "`Aantal` is disabled on the woon-werk form"
    One row is one day's travel both ways, fixed at `1,00` by the declaration
    profile. `set_or_verify_quantity()` checks `is_disabled()` first and asserts
    the value rather than writing it. Another profile might leave it editable, so
    the code adapts rather than assuming either way.

## Why scrape the calendar

This tenant grants no app registrations, so Microsoft Graph is unavailable. The
alternatives were a published-ICS capability URL, which is world-readable and
silently truncates to a rolling three-month window, or borrowing the access token
OWA mints in-page for its own first-party client, which is the token-replay shape
security tooling is built to flag. Driving the page as the signed-in user is the
honest option, and it reuses the Entra session the job already holds.

The fragile half, parsing `aria-label` strings, is kept out of the browser module
so it can be unit-tested against captured strings with no login required.

## Session lifetime

When the tenant suppresses the "Stay signed in?" prompt, Entra issues a
**non-persistent** session cookie that is discarded when the browser process
exits. A profile on disk therefore does not carry a usable session between runs,
and every run signs in afresh. That is acceptable at this cadence, and it is why
there is no session-keeper workload and why the browser profile is not a PVC.

## Security posture

- The 1Password service-account token is never read by application code. `op`
  reads it from the process environment itself.
- TOTP codes are fetched at the moment they are typed, never up front. The window
  is 30 seconds and page loads eat most of it, which is why `Credentials.totp` is
  a callable.
- `browser-profile/`, `traces/` and `artifacts/` are credential-grade: a saved
  profile is a replayable, MFA-satisfied corporate session and a trace holds full
  DOM, headers and cookies. They are gitignored and excluded from the Docker
  build context.
- `scripts/explore_insite.py` is read-only and never clicks a submit control.
- Pods run non-root, `seccompProfile: RuntimeDefault`, all capabilities dropped,
  read-only root filesystem where feasible.
