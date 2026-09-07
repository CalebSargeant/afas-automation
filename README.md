# AFAS Declaraties

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Automated monthly travel and working-from-home expense claims for AFAS InSite.
It reads the work calendar out of Outlook Web, decides day by day whether that
was a commute or a day at home, keeps the decisions in a Postgres ledger, and
drives the InSite *verzameldeclaratie* form with Playwright to file the claim.
Anything ambiguous becomes a Slack question, and the irreversible submission
happens only after a human approves it.

This repository is public and contains **no employer identifiers**. Every example
value is a `<PLACEHOLDER>`; the real ones live in OCI Vault and cluster secrets.

## Features

- 📅 **Evidence-based classification**: an office day needs three independent
  signals to agree (all-day event, booking organiser, subject prefix), so a false
  positive needs three coincidences. Home is the residual.
- 🧾 **Postgres is the system of record**: AFAS is an output format. The ledger
  keeps every verdict with its reason codes and evidence, so "why was 12 August
  claimed?" has an answer three weeks later.
- 🚫 **Fails towards claiming less**: an outage, a contradiction or a withdrawn
  booking becomes `needs_input` and a question, never a guess. An omitted day
  costs a couple of euros; a wrong day is a false expense claim.
- 🔐 **Headless Microsoft SSO**: Entra sign-in driven as a state machine
  (account picker → email → password → TOTP → stay-signed-in, in whatever order
  the tenant offers), with credentials fetched from 1Password at the moment they
  are typed.
- 💬 **Slack in the loop**: a weekly digest, a day-correction modal, and a
  monthly approval card whose Approve button is bound to a digest of exactly
  which days would be filed. Socket Mode, so no inbound endpoint.
- 🛡️ **Dry run by default**: `DRY_RUN=true` ships as the default and gates the
  path that spends real money.
- 🐳 **Container and chart in one repo**: multi-arch image plus an in-repo Helm
  chart, released in lockstep.

## How It Works

### 1. Classify (nightly)

Reads the work week and turns each entry into a normalised event. There are two
readers and they answer the same `(events, degraded)` contract, so the
classifier cannot tell them apart:

| `CALENDAR_SOURCE` | How it reads |
|---|---|
| `owa` (default) | Drives Outlook Web with Chromium as the signed-in user and parses each `aria-label` |
| `mcp` | Calls the Microsoft 365 MCP connector over HTTPS. No browser, no SSO sign-in, no saved profile |

`mcp` is the better path where it is available. It removes the corporate SSO
login that must never be retried, the `browser-profile/` directory (a
replayable, MFA-satisfied session on disk), and Chromium itself from the
nightly job. It needs one interactive sign-in to mint a refresh token; see
[docs/setup.md](docs/setup.md#the-microsoft-365-mcp-connector).

There is deliberately no automatic fallback between the two. Silently swapping
the source a claim is derived from is how "the read failed" becomes "there were
no office days".

Every working day then gets a verdict:

| Evidence | Verdict | Claim |
|---|---|---|
| Weekend or a configured non-working day | `absent` | none |
| Out-of-office in the calendar, or leave in AFAS | `absent` | none |
| Desk booking present (all-day + organiser + subject prefix) | `office` | commute |
| No booking | `home` | working from home |
| Calendar read degraded | `ambiguous` | ask a human |
| Booking withdrawn since a previous run froze the day as office | `ambiguous` | ask a human |

Each verdict is written to the `claim_day` table with its reason codes and
evidence. Days that are already `drafted`, `submitted` or human-overridden are
never rewritten.

### 2. Digest (weekly)

Posts the week to Slack: what was classified, what is still unresolved, and a
button to open a day-correction modal. Corrections land in the ledger as human
overrides, which subsequent nightly runs will not overwrite.

### 3. Build (monthly)

Takes the claimable days for the period and enters them on the InSite
verzameldeclaratie form, one row per day. Each row sets the booking date, the
period and the quantity, then **reads the form back and verifies it** before the
row is committed. `Aanmaken` produces a draft task that the employee can still
amend or delete.

### 4. Approve

The draft is posted to Slack with a fingerprint (`lines_digest`) of exactly which
days it contains, and the Approve button carries that fingerprint. If the set of
days changed after the card was posted, the fingerprint moves and the approval is
refused rather than applied to different content. Approvals are also deduplicated
in Postgres, because Socket Mode redelivers on reconnect.

> **Not yet implemented:** the final *Declaratie insturen* click. The path
> through the task list has been identified but not verified end to end against
> the live portal, and it is the one irreversible step in the system, so
> `cli submit` raises rather than shipping on the strength of a plausible guess.
> Until then the last step is done by hand on the draft task.

## Configuration

All configuration is environment variables. In production the secret ones arrive
from OCI Vault via External Secrets Operator, and the rest from a ConfigMap.

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | Yes | - | Postgres DSN for the claim ledger, e.g. `postgresql://<PLACEHOLDER>`. Carries the password, so it is a credential |
| `INSITE_HOST` | Yes | - | AFAS InSite hostname, no scheme, e.g. `<PLACEHOLDER>` |
| `COMMUTE_PAGE_PATH` | Yes | - | Page slug for the commute (woon-werk) form, e.g. `<PLACEHOLDER>` |
| `HOME_PAGE_PATH` | Yes | - | Page slug for the working-from-home (thuiswerk) form, e.g. `<PLACEHOLDER>` |
| `OP_SERVICE_ACCOUNT_TOKEN` | Yes | - | 1Password service-account token (`ops_<PLACEHOLDER>`). Read by the `op` CLI itself, never by this code |
| `OP_ITEM_NAME` | No | `Microsoft` | 1Password item holding the Microsoft credentials. Must expose `username`, `password` and a configured TOTP |
| `OP_VAULT` | No | `Private` | 1Password vault name |
| `SLACK_BOT_TOKEN` | For Slack | - | `xoxb-<PLACEHOLDER>`. Needs `chat:write` |
| `SLACK_APP_TOKEN` | For Slack | - | `xapp-<PLACEHOLDER>`, app-level token with scope `connections:write` (Socket Mode) |
| `SLACK_CHANNEL_ID` | For Slack | - | Channel the digest and approval card are posted to, e.g. `<PLACEHOLDER>` |
| `SLACK_APPROVER_IDS` | If not dry run | - | Comma-separated Slack user ids allowed to approve. **Case-sensitive, never normalised.** Empty with `DRY_RUN=false` is a refusal to start |
| `DRY_RUN` | No | `true` | `false` actually creates declarations. This spends real money |
| `LOG_LEVEL` | No | `INFO` | Standard Python log level |
| `BROWSER_PROFILE_DIR` | No | `/tmp/browser-profile` | Chromium user-data dir. Must sit on a writable volume; the root filesystem is read-only |
| `CALENDAR_SOURCE` | No | `owa` | `owa` drives Outlook Web with Chromium; `mcp` reads the calendar over the Microsoft 365 MCP connector. Anything else is refused |
| `M365_TOKEN_JSON` | If `mcp` | - | The MCP token cache as JSON, straight from `m365_mcp login`. A refresh token, so it is a credential |
| `M365_TOKEN_CACHE` | No | `~/.m365-mcp-token.json` | Where that JSON is written. Must be writable: Entra rotates the refresh token on use |
| `M365_TOKEN_SEED` | No | - | A file holding the same JSON, if you would rather mount it than pass it as an env var |
| `M365_TENANT` | No | `organizations` | Entra authority segment. Only set it if sign-in resolves the wrong directory |
| `REQUIRE_CALENDAR_EVENTS` | No | `true` | Treat a working window with no calendar entries at all as a question, not as a week spent at home |
| `BOOKING_ORGANISERS` | No | the desk-booking tool | Comma-separated organiser names that mark a desk booking |
| `BOOKING_SUBJECT_PREFIXES` | No | `Booking` | Comma-separated subject prefixes, matched case-insensitively |
| `EXCLUDED_DATES` | No | - | Comma-separated ISO dates never claimed (leave, holidays the calendar does not carry) |
| `OP_DOCKER_IMAGE` | No | - | **Local dev only.** Runs the `op` CLI in a container so it does not block on a locked 1Password desktop app. Leave unset in the cluster |

Nothing has a tenant-specific default. Every identifying value arrives from the
environment, because this repository is public.

## Deployment

The image is `ghcr.io/calebsargeant/afas-declaraties`, built for `linux/arm64`
(the target node pool) and `linux/amd64`. The Helm chart lives in this repo at
`charts/afas-declaraties/` and is released in lockstep with the image.

```bash
helm lint charts/afas-declaraties
helm template afas-declaraties charts/afas-declaraties \
  -f charts/afas-declaraties/ci/default-values.yaml
```

Workloads, all in `Europe/Amsterdam`:

| Workload | Kind | Schedule |
|---|---|---|
| `classify` | CronJob | nightly 23:30 |
| `digest` | CronJob | Fridays 16:00 |
| `build` | CronJob | 28th of the month 06:00 |
| `submit` | CronJob | daily 07:00 |
| `slackd` | Deployment | always on |

Every browser job runs with `concurrencyPolicy: Forbid`, `backoffLimit: 0`, an
`activeDeadlineSeconds` and `startingDeadlineSeconds: 600`. **Browser jobs are
never retried**: a retried corporate SSO login risks account lockout.

`slackd` is exactly one replica with `strategy: Recreate`, no Service and no
Ingress. Slack Socket Mode dials out, and Slack load-balances events across an
app's sockets, so a second replica would silently drop half the clicks. Health is
an exec probe against a heartbeat file at `/tmp/healthy`.

Pods run non-root with `seccompProfile: RuntimeDefault`, all capabilities
dropped, a read-only root filesystem where feasible, a writable `/tmp` emptyDir
and a `/dev/shm` emptyDir (Chromium crashes on the default 64 MB).

Secrets are delivered by External Secrets Operator against the estate's
`ClusterSecretStore`; non-secret configuration is a ConfigMap.

## Troubleshooting

- **`op` hangs, then times out after 30s, locally**: the 1Password CLI is waiting
  on the locked desktop app. Set `OP_DOCKER_IMAGE=<PLACEHOLDER>` to run it in a
  container instead. In the cluster the same timeout means
  `OP_SERVICE_ACCOUNT_TOKEN` is missing or invalid.
- **Sign-in fails with an empty password**: Entra keeps every pane in the DOM at
  once, so visibility alone cannot say which step is on screen. See
  `.claude/COMMON_MISTAKES.md` entry 1.
- **A claim landed in the wrong payroll month**: `Periode` prefills to the
  *current* month, not the booking date's month. Every row sets it explicitly and
  verifies it. See `.claude/COMMON_MISTAKES.md` entry 4.
- **`PortalChanged` raised**: a control could not be found at all, meaning InSite
  moved. Stop and re-run `scripts/explore_insite.py` to dump the live form. Do
  not retry.
- **Everything classified as home for a whole week**: check for
  `Reason.CALENDAR_DEGRADED` in the ledger. A degraded calendar read is meant to
  produce `needs_input`, never a home day.
- **Slack buttons do nothing**: `slackd` must be exactly one replica, and the
  clicking user's id must be in `SLACK_APPROVER_IDS` (case-sensitive).

Run `python -m pytest` for the fast feedback loop; the classifier and calendar
parser tests need no browser, network or database.

## License

MIT License - see [LICENSE](LICENSE) file for details.
