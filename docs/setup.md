# Setup & Deployment

## Configuration

Everything is environment variables. In the cluster the secret ones arrive from
OCI Vault via External Secrets Operator; the rest come from a ConfigMap.

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | yes | - | Postgres DSN for the claim ledger. Carries the password, so it is a credential and never appears in a values file. |
| `INSITE_HOST` | yes | - | AFAS InSite hostname, no scheme. |
| `COMMUTE_PAGE_PATH` | yes | - | Page slug for the commute (woon-werk) form. Employer-chosen publication name. |
| `HOME_PAGE_PATH` | yes | - | Page slug for the working-from-home (thuiswerk) form. |
| `OP_SERVICE_ACCOUNT_TOKEN` | yes | - | 1Password service-account token (`ops_<PLACEHOLDER>`). Read by `op` itself. |
| `OP_ITEM_NAME` | no | `Microsoft` | 1Password item holding the Microsoft credentials. |
| `OP_VAULT` | no | `Private` | 1Password vault name. |
| `SLACK_BOT_TOKEN` | for Slack | - | `xoxb-<PLACEHOLDER>`, scope `chat:write`. |
| `SLACK_APP_TOKEN` | for Slack | - | `xapp-<PLACEHOLDER>`, app-level token, scope `connections:write`. |
| `SLACK_CHANNEL_ID` | for Slack | - | Channel for the digest and approval card. |
| `SLACK_APPROVER_IDS` | if not dry run | - | Comma-separated Slack user ids. **Case-sensitive, never normalised.** |
| `DRY_RUN` | no | `true` | `false` creates real declarations. |
| `LOG_LEVEL` | no | `INFO` | Python log level. |
| `BROWSER_PROFILE_DIR` | no | `/tmp/browser-profile` | Chromium user-data dir. Must be on a writable volume. |
| `CALENDAR_SOURCE` | no | `owa` | `owa` drives Outlook Web with Chromium; `mcp` reads the calendar over the Microsoft 365 MCP connector. Anything else is refused rather than defaulted. |
| `M365_TOKEN_JSON` | if `mcp` | - | The MCP token cache as JSON. Holds a refresh token, so it is a credential. |
| `M365_TOKEN_CACHE` | no | `~/.m365-mcp-token.json` | Where that JSON is written. Must be writable: Entra rotates the refresh token on use. |
| `M365_TOKEN_SEED` | no | - | Path to a file holding the same JSON, if a mount suits better than an env var. |
| `M365_TENANT` | no | `organizations` | Entra authority segment. Pin it only if sign-in resolves the wrong directory. |
| `REQUIRE_CALENDAR_EVENTS` | no | `true` | A working window with no calendar entries at all becomes a question, not a week at home. |
| `BOOKING_ORGANISERS` | no | the desk-booking tool | Comma-separated organiser names that mark a desk booking. |
| `BOOKING_SUBJECT_PREFIXES` | no | `Booking` | Comma-separated subject prefixes, case-insensitive. |
| `EXCLUDED_DATES` | no | - | Comma-separated ISO dates never claimed. |
| `OP_DOCKER_IMAGE` | no | - | Local dev only. See below. |

Nothing here has a tenant-specific default. `Config.from_env()` is a frozen
dataclass built once at startup, and a missing required value is a clean exit
code 2 with the variable named, not a traceback halfway through a browser run.

The 1Password item must expose `username`, `password` and a configured **TOTP**.
`get_field()` matches on both the field `id` and its `label`, because built-in
fields carry stable ids while custom fields only have a label.

!!! danger "`DRY_RUN` gates a path that spends real money"
    It defaults to `"true"` in `values.yaml` and in the application. Flipping it
    is a deliberate act, never a side effect of another change. With
    `DRY_RUN=false` and an empty `SLACK_APPROVER_IDS`, `slackd` refuses to start:
    an empty allowlist means anyone can approve a money path.

## Local development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
playwright install chromium

export DATABASE_URL=postgresql://<PLACEHOLDER>
export INSITE_HOST=<PLACEHOLDER>
export COMMUTE_PAGE_PATH=<PLACEHOLDER> HOME_PAGE_PATH=<PLACEHOLDER>
export OP_ITEM_NAME=<PLACEHOLDER> OP_VAULT=<PLACEHOLDER>
export OP_SERVICE_ACCOUNT_TOKEN=ops_<PLACEHOLDER>
export DRY_RUN=true LOG_LEVEL=DEBUG
```

Then run a command:

```bash
python -m afas_declaraties.cli classify --window 9
python -m afas_declaraties.cli digest --week 2026-08-31
python -m afas_declaraties.cli build --period 2026-08
python -m afas_declaraties.cli slackd
python -m afas_declaraties.cli override 2026-08-31 office --actor <PLACEHOLDER>
```

`override` takes a verdict of `office`, `home` or `absent` and prints whether it
was applied or refused (a day already submitted is refused, exit code 1). It is
the escape hatch for when Slack is unavailable.

`pyproject.toml` carries loose ranges for humans; `requirements.txt` pins every
runtime dependency and its transitives, and is what the image installs. A
browser-driving job that logs into corporate SSO must not change behaviour
because a transitive floated.

### The `op` CLI on a workstation

On a workstation the 1Password CLI talks to the desktop app over
`~/.config/op/op-daemon.sock` and **blocks forever when the app is locked**, even
with a valid service-account token and a clean environment. The symptom is a
30-second timeout from `onepassword._run()` with nothing useful in the output.

```bash
export OP_DOCKER_IMAGE=<PLACEHOLDER>
```

`onepassword._command()` then runs `op` inside a container, which has no desktop
app to fall back to, so the token is used directly. This is also exactly how the
CLI runs in production. The token is passed by name (`-e OP_SERVICE_ACCOUNT_TOKEN`)
so it never appears in the container's argv, which is world-readable via `ps`.

Leave `OP_DOCKER_IMAGE` **unset** in the cluster. There is no docker socket
there, and the same timeout in a pod means the service-account token is missing
or invalid.

### Seeding a browser session

```bash
# Sign in by hand. Nothing is typed for you.
python scripts/bootstrap_session.py --manual

# Automated, headed, with a Playwright trace
python scripts/bootstrap_session.py --headed --trace traces/bootstrap.zip
```

A run that finds a live session is a silent redirect and enters no credentials at
all.

!!! danger "The browser profile is a credential"
    `browser-profile/` is a replayable, MFA-satisfying corporate session.
    `traces/` holds full DOM, headers and cookies. `artifacts/` holds screenshots
    and scraped data from the employer portal. All three are gitignored and
    excluded from the Docker build context. Never upload one, attach one to an
    issue, or copy one off the machine it was created on.

### The Microsoft 365 MCP connector

The alternative to driving Outlook Web for the calendar, and the one that needs
no browser at all. Set `CALENDAR_SOURCE=mcp`.

**Why it works without an app registration.** This tenant grants no app
registrations, so the ordinary Microsoft Graph route is closed. Anthropic
registered a multi-tenant client/server app pair for its own Microsoft 365
connector, the tenant has already consented to it, and a device-code sign-in
against that pair therefore needs no admin action. Every scope is delegated, so
the reach is capped at what the signed-in user can already open in Outlook.
Nothing is escalated; the calendar just becomes scriptable.

```bash
# Once, interactive. Prints a code to type into a browser.
python -m afas_declaraties.m365_mcp login

# Check what it can see
python -m afas_declaraties.m365_mcp call get_me
python -m afas_declaraties.m365_mcp tools
```

That writes `~/.m365-mcp-token.json`. Put its **contents** in the vault as the
`M365_TOKEN_JSON` entry and add it to `externalSecret.data`; the chart refuses
to render with `calendarSource: mcp` and no source for it.

```bash
export CALENDAR_SOURCE=mcp
python -m afas_declaraties.cli classify --since 2026-09-01 --until 2026-09-05
```

!!! danger "The token cache is a credential"
    A refresh token is standing read access to the whole mailbox that never
    re-prompts for MFA, and it renews itself for as long as it keeps being
    used. It is the same class of thing as `browser-profile/`. Gitignored,
    excluded from the image, and it never leaves the vault.

!!! note "Why the cache is copied to /tmp"
    Entra rotates the refresh token on every use, and a mounted Secret is
    read-only. The chart therefore seeds `M365_TOKEN_CACHE` (`/tmp/...`) from
    `M365_TOKEN_JSON` at the start of each run and lets the rotation land
    there. Losing that rotation when the pod exits is harmless -- the seed
    stays valid for its own sliding window -- but the token does have to be
    used at least once every 90 days or it ages out.

!!! warning "Two tenants, and the wrong one looks right"
    `M365_TENANT` defaults to `organizations`, which lets Entra resolve the
    signed-in user's home directory. An account that is a guest in a second
    tenant can land in that one instead, and querying the wrong directory gives
    confidently wrong answers rather than an error. If `get_me` comes back
    looking odd, pin `M365_TENANT` to the home tenant id.

### Inspecting the portal

```bash
python scripts/explore_insite.py / --shot artifacts/home.png --out artifacts/home.json
python scripts/explore_insite.py <PATH> --headed --net --wait 10
```

Read-only. It walks light DOM **and open shadow roots** together, so a control
rendered inside a custom element is reported with the id its inner `<input>`
actually carries. It never clicks a submit control. Run this first whenever
`PortalChanged` is raised.

## Tests

```bash
python -m pytest
ruff check . && ruff format --check .
```

`tests/test_classify.py` and `tests/test_calendar_owa.py` need no browser, no
network and no database. They are the fast feedback loop and cover the two rules
that are most expensive to get wrong: a degraded calendar read must never become
a home day, and a withdrawn booking must ask rather than assume.

Calendar fixtures reproduce the real `aria-label` structure exactly while every
value in them is invented. The shape is what matters, because
the shape is the thing under test.

## Container

```bash
docker buildx bake --set '*.platform=linux/arm64,linux/amd64'
docker build -t afas-declaraties:local .
```

The image is `ghcr.io/calebsargeant/afas-declaraties`. **`linux/arm64` is the
target**, the cloud tier is arm64 nodes; amd64 is built as well.

## Kubernetes

The Helm chart is in-repo at `charts/afas-declaraties/` and is released in
lockstep with the image, so `appVersion` always names a tag that exists.

```bash
helm lint charts/afas-declaraties
helm template afas-declaraties charts/afas-declaraties \
  -f charts/afas-declaraties/ci/default-values.yaml
```

### Workloads

| Workload | Kind | Schedule (Europe/Amsterdam) | Notes |
|---|---|---|---|
| `classify` | CronJob | nightly 23:30 | browser |
| `digest` | CronJob | Fridays 16:00 | |
| `build` | CronJob | 28th of the month 06:00 | browser, respects `DRY_RUN` |
| `submit` | CronJob | daily 07:00 | browser, respects `DRY_RUN`; no-op unless a period is approved |
| `slackd` | Deployment | always on | exactly 1 replica |

All CronJobs set `spec.timeZone: Europe/Amsterdam` so the schedule stays correct
across DST.

### Browser job safety

Every browser job runs with:

- `concurrencyPolicy: Forbid`
- `backoffLimit: 0`
- an explicit `activeDeadlineSeconds`
- `startingDeadlineSeconds: 600`

!!! danger "Never retry a browser job"
    Retrying a corporate SSO login risks account lockout. `backoffLimit: 0` is
    not a tuning knob. The Postgres advisory lock in `store.browser_lock()` backs
    this up at runtime: a job that cannot take the lock exits cleanly rather than
    queueing behind one stuck in an SSO flow.

### `slackd`

Exactly **one** replica with `strategy: Recreate`. No Service, no Ingress: Slack
Socket Mode dials out. Slack load-balances events across an app's sockets, so a
second replica silently drops half the clicks, and a `PodDisruptionBudget` with
`minAvailable: 1` on a single-replica Deployment blocks node drains forever.

Health is an **exec probe** against a heartbeat file at `/tmp/healthy`, touched
on each successful connection and every 60 seconds. There is no port to probe.

### Pod security and Chromium

- non-root user, `seccompProfile: RuntimeDefault`, all capabilities dropped
- `readOnlyRootFilesystem` where feasible, with a writable `/tmp` `emptyDir`
- `/dev/shm` as an `emptyDir` (Chromium crashes on the default 64 MB), which
  counts against the container's memory limit, so the limit must exceed the shm
  size plus the browser's working set

### Secrets

Secrets come from External Secrets Operator against the `ClusterSecretStore` the
estate already uses; non-secret configuration goes in a ConfigMap. The chart
never templates a credential value, and every identifying value defaults to `""`
in `values.yaml`.

ESO resolves a `data:` list **all or nothing**: one missing entry leaves the
entire Secret absent and every pod down. Create all of them before the infra PR
opens.

## CI/CD

1. Push to `main` triggers semantic-release, which reads Conventional-Commit
   messages and cuts a versioned GitHub release. `CHANGELOG.md` is generated;
   never hand-edit it.
2. Release published triggers the multi-arch image build and push to GHCR.
3. The chart is packaged and published alongside it.

Branch names follow `<type>/<description>` or `<type>/<scope>/<description>`,
with the standard Conventional-Commit types.

### Public-repo hygiene

This repository is public. CI enforces that no tracked file contains the employer
name, the AFAS tenant number or hostname, an Entra tenant GUID, Slack workspace,
channel or user ids, OCI OCIDs, vault names, email addresses or employee numbers,
and that `browser-profile/`, `traces/` and `artifacts/` never appear in
`git ls-files`.

Safe to commit: vault entry **names**, Kubernetes Secret and key names, env var
names, database and role names, chart manifest shapes, the Slack scope list, and
Block Kit JSON with placeholder text.

## Docs

```bash
mkdocs serve     # preview at http://127.0.0.1:8000
mkdocs build     # static site -> ./site (gitignored)
```

No screenshots of the portal, ever.
