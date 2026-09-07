# Architecture map

Python package `src/afas_declaraties/`, entrypoint `python -m afas_declaraties.cli`.
Commands: `classify`, `digest`, `build`, `submit`, `slackd`, `override`.
`submit` is LIVE when `DRY_RUN=false` -- it clicks the irreversible
*Declaratie insturen* action. That click has never been executed against the
live portal; everything before it has.

## Modules

| Module | Role | I/O |
|---|---|---|
| `cli.py` | argparse dispatch, one command per workload. Builds `ClassifierConfig` from `Config`, takes the browser lock, drives the rest. | - |
| `config.py` | `Config.from_env()`, frozen dataclass. No tenant-specific defaults. Refuses to start with an empty `SLACK_APPROVER_IDS` when `DRY_RUN=false`. | env |
| `models.py` | Domain types: `ClaimType`, `DayState`, `Verdict`, `Reason`, `CalendarEvent`, `DayClassification`. `CalendarEvent.is_desk_booking()` needs three signals to agree (all-day + organiser + subject prefix). | none, by design |
| `classify.py` | `classify_day()` turns evidence into a verdict. `ClassifierConfig` holds everything person/tenant-specific. Rules are asymmetric: office needs positive evidence, home is the residual, contradictions become a question. | none |
| `calendar_owa.py` | Reads Outlook Web. `parse_event_label()` (pure, unit-tested against captured `aria-label` strings) + `read_week()` which returns `(events, degraded)`. | Playwright |
| `calendar_mcp.py` | Reads the same calendar over the M365 MCP connector. `parse_event()` (pure, unit-tested against captured payloads) + `read_range()`, same `(events, degraded)` contract. All-day events expand across the days they span; a failed or empty read is degraded, never "no office days". | HTTPS |
| `m365_mcp.py` | The connector transport. Device-code sign-in against Anthropic's multi-tenant app pair, refresh-token cache, JSON-RPC, and `search()` which pages and reports whether the answer is complete. Stdlib only. | HTTPS, Entra |
| `entra.py` | Microsoft Entra sign-in as a **state machine**: account tiles / email / password / TOTP / stay-signed-in, in whatever order the tenant offers them. `_is_active()` is the whole trick (see COMMON_MISTAKES #1). | Playwright |
| `session.py` | `open_session()` context manager: persistent Chromium context, sign in if needed, `wait_until_settled()` on host **and** non-transient path, yield `(context, page)`. | Playwright, 1Password |
| `onepassword.py` | `get_field()` / `get_totp()` via the `op` CLI. `OP_DOCKER_IMAGE` runs it in a container for local dev. Token is read by `op` from the env, never by this module. | subprocess |
| `insite.py` | Drives the AFAS verzameldeclaratie form. `add_row` → `fill_row` (`set_text_field` / `set_reference` / `set_or_verify_quantity`) → `verify_row` → `confirm_row`, then `create_draft`. `PortalChanged` vs `InSiteError` is the retry/stop distinction. | Playwright |
| `store.py` | The claim ledger on the shared CNPG Postgres. Raw SQL, idempotent DDL, no ORM. `claim_day` is keyed by **date**, so a day physically cannot hold both a commute and a WFH claim. `browser_lock()` is a `pg_try_advisory_lock`. `lines_digest()` binds a Slack approval to exact content. `claim_slack_interaction()` dedupes Socket Mode redeliveries. | Postgres |
| `slack_blocks.py` | Block Kit builders: `weekly_digest()`, `approval_card()`. Pure. | none |
| `slackd.py` | Socket Mode app. Acks first, writes one row, touches the `/tmp/healthy` heartbeat. Never drives Playwright. | Slack, Postgres |

## Data flow

`CALENDAR_SOURCE` picks the reader. Both answer `(events, degraded)`, and there
is no fallback between them: swapping the source a claim is derived from
without saying so is how "the read failed" becomes "there were no office days".

```
OWA week  --read_week--.
                       |
M365 MCP  --read_range-+-->  CalendarEvent[]  --classify_day-->  DayClassification
                                                                     |
                                                              store.record_day
                                                                     v
                                                    claim_day (planned/needs_input/…)
                                                                     |
                          Slack digest + override  <---- unresolved_days
                                                                     |
                                        claimable_days --> insite rows --> Aanmaken
                                                                     |
                                          Slack approval (lines_digest) --> insturen
```

`AFAS is an output format, not the system of record.` The ledger keeps knowing
what was worked where when a portal change, a rejected line or a lost session
loses the rest.

## State machine

`DayState`: `planned → confirmed → drafted → submitted`, with `needs_input`,
`excluded` and `failed` off to the side. `record_day` refuses to overwrite a day
that is `submitted`/`drafted` or carries an override; `apply_override` refuses a
`submitted` day.

## Scripts (dev only)

- `scripts/bootstrap_session.py` seeds/refreshes `browser-profile/`. `--manual`
  opens a headed browser and types nothing for you.
- `scripts/explore_insite.py` dumps links, fields (through open shadow roots),
  custom elements, frames and XHR to `artifacts/`. Read-only, never submits.

Both write credential-grade output. `browser-profile/`, `traces/`, `artifacts/`
are gitignored and `.dockerignore`d.

## Deployment

Image `ghcr.io/calebsargeant/afas-declaraties`, `linux/arm64` (+ amd64). Helm
chart in-repo at `charts/afas-declaraties/`. CronJobs `classify` (23:30 nightly),
`digest` (Fri 16:00), `build` (28th 06:00), all Europe/Amsterdam,
`concurrencyPolicy: Forbid`, `backoffLimit: 0`. Deployment `slackd`, exactly 1
replica, `Recreate`, no Service, no Ingress (Socket Mode dials out), exec probe
on `/tmp/healthy`. Secrets via External Secrets Operator; non-secret config in a
ConfigMap. `/dev/shm` emptyDir for Chromium.
