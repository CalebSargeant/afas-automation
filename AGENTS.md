# AGENTS.md

Guidance for AI coding agents working in this repository. Claude Code should read
`CLAUDE.md` first, which is deliberately terse and points at `.claude/*.md`; this
file is the longer prose version for agents that want background.

## Repository overview

`afas-declaraties` automates a monthly Dutch expense claim end to end:

1. Read the work calendar out of Outlook Web.
2. Classify every working day as **commute** (office) or **home**
   (working-from-home), or refuse to decide.
3. Persist the decision, with its evidence, in a Postgres ledger.
4. Ask a human in Slack about anything ambiguous.
5. Drive the AFAS InSite *verzameldeclaratie* form with Playwright to build a
   draft declaration, one row per day.
6. Submit only after an explicit human approval in Slack.

Step 6's final click (*Declaratie insturen*) **is implemented and live**.
`cli submit` clicks it whenever `DRY_RUN=false`. It is the one irreversible step
in the system, so it is gated three ways: `DRY_RUN` defaults to true, an
allow-listed Slack user must approve, and the approval's `lines_digest` is
re-derived from the ledger and must still match. The task is chosen by matching
its form slug; two open drafts of one claim type are refused rather than
guessed. Everything except that last click has been exercised against the live
portal -- the click itself has not, because doing so files a real claim.

Layout:

- `src/afas_declaraties/` - the package. Entrypoint module `afas_declaraties.cli`,
  invoked as `python -m afas_declaraties.cli <command>`.
  Commands: `classify`, `digest`, `build`, `submit`, `slackd`, `override`.
- `scripts/` - developer reconnaissance tooling. Not shipped, not scheduled.
- `tests/` - pytest. The two suites that exist run with no browser, no network
  and no database.
- `charts/afas-declaraties/` - the Helm chart, in-repo, released alongside the
  image.
- `docs/` - the published MkDocs site.

## Core architecture

### Domain layer (no I/O)

`models.py` and `classify.py` contain no I/O at all, which is what makes the
rules testable without a browser, a database or a Slack workspace. Keep it that
way: if a rule needs a new input, pass it as an argument rather than reaching for
a client.

- `ClaimType` is `commute | home`. They are mutually exclusive per day
  (art. 31a lid 12 Wet LB 1964), and that is enforced structurally: `claim_day`
  is keyed by date, so one day cannot physically hold both.
- `Verdict` is what the classifier thinks (`office | home | absent | ambiguous`);
  `DayState` is where the day is in its lifecycle
  (`planned | needs_input | confirmed | excluded | drafted | submitted | failed`).
- Every verdict carries `Reason` codes and an `evidence` dict. A verdict without
  them is undebuggable three weeks later, which is exactly when someone asks why
  a day was or was not claimed.

The rules are deliberately **asymmetric**. An office day needs positive evidence
(an all-day event, from the booking organiser, whose subject starts with the
booking prefix, three independent signals, so a false positive needs three
coincidences). Home is the residual. Anything contradictory becomes a question
for a human. The failure direction is always to claim **less**.

Two rules are worth stating separately because both guard against silent,
expensive failures:

- A **degraded** calendar read returns `AMBIGUOUS`, never "no bookings, therefore
  home". An outage must not become a month of wrong claims. A day that a good
  read already decided keeps its verdict.
- A booking that **disappeared** after a previous run froze the day as office
  returns `AMBIGUOUS` too. Deleting the calendar entry is how the user says "I
  did not actually go in", and that signal must neither be lost nor acted on
  silently.

### Browser layer

`entra.py` drives Microsoft Entra sign-in as a **state machine**, not a fixed
sequence: the tenant varies the order and omits steps depending on what the
profile already holds, so an account picker, a skipped password or an absent TOTP
prompt are ordinary states. `session.py` wraps that in `open_session()`, which
yields a `(context, page)` that is signed in **and settled** (right host, and not
on a transient callback path).

`calendar_owa.py` scrapes Outlook Web because this tenant grants no app
registrations, so Microsoft Graph is unavailable. The parsing half is pure and
unit-tested against captured `aria-label` strings; only `read_week()` touches a
page.

`insite.py` drives the declaration form. InSite renders controls as custom
elements with open shadow roots, so ordinary selectors see wrappers and nothing
useful. The module is small on purpose and every write is read back before the
next step.

### Ledger

`store.py` is raw SQL with idempotent DDL against the shared CNPG Postgres. No
ORM, no migration tool, matching the house pattern in `github-timesheet`.

The ledger is the **system of record**; AFAS is an output format. A portal
change, a rejected line or a lost session must never lose the knowledge of what
was worked where. `record_day` will not overwrite a day that is already
`drafted`/`submitted` or carries a human override. `apply_override` will not
touch a `submitted` day. `lines_digest()` fingerprints exactly what would be
filed so a Slack approval is bound to specific content and cannot be replayed
against a different set of days.

`browser_lock()` is a Postgres advisory lock that **yields `False` rather than
blocking**, so a job that cannot get it exits cleanly instead of queueing behind
one stuck in an SSO flow.

## Rules an agent must not break

1. **This repo is public.** No employer name, AFAS tenant number or hostname,
   Entra tenant GUID, Slack workspace/channel/user id, OCI OCID, vault name,
   email address or employee number in any tracked file. Use `<PLACEHOLDER>` in
   every example. Real values live in OCI Vault and cluster secrets only.
2. **`DRY_RUN` defaults to `"true"`** and gates a path that spends real money.
   Do not change the default, and do not add a code path that bypasses it.
3. **Never retry a browser job.** `concurrencyPolicy: Forbid`, `backoffLimit: 0`,
   an `activeDeadlineSeconds`, `startingDeadlineSeconds: 600`. Retrying a
   corporate SSO login risks account lockout.
4. **Never select an InSite control by its label.** Two buttons read "Aanmaken"
   and one of them files the declaration. Ids only.
5. **Never write a value without reading it back.** Both Entra and InSite
   silently discard values typed a moment earlier.
6. **Never let an error path produce a claim.** Errors become `needs_input` and a
   Slack question, never a default.
7. `browser-profile/`, `traces/` and `artifacts/` are credential-grade. They stay
   gitignored and out of the Docker build context.

## Common commands

```bash
pip install -e '.[dev]' && playwright install chromium
python -m pytest                       # unit tests, no browser/network/db
ruff check . && ruff format --check .
python -m afas_declaraties.cli classify --window 9
python -m afas_declaraties.cli override 2026-08-31 office --actor <PLACEHOLDER>
python scripts/bootstrap_session.py --manual             # seed browser-profile/
python scripts/explore_insite.py / --out artifacts/home.json
helm lint charts/afas-declaraties
docker buildx bake --set '*.platform=linux/arm64,linux/amd64'
```

See `.claude/QUICK_START.md` for the full list with the environment each command
needs.

## Deployment model

One image, six commands, five workloads.

| Workload | Kind | Schedule (Europe/Amsterdam) |
|---|---|---|
| `classify` | CronJob | nightly 23:30 |
| `digest` | CronJob | Fridays 16:00 |
| `build` | CronJob | daily 06:00, acts when a month is due |
| `submit` | CronJob | daily 07:00 |
| `slackd` | Deployment | always on, exactly 1 replica |

`slackd` runs Slack Socket Mode, which dials out, so it has no Service and no
Ingress and its health is an exec probe on a heartbeat file at `/tmp/healthy`.
Exactly one replica with `strategy: Recreate`: Slack load-balances events across
an app's sockets, so a second replica silently drops half the clicks.

Secrets arrive via External Secrets Operator against the estate's
`ClusterSecretStore`; non-secret config lives in a ConfigMap. Pods run non-root
with `seccompProfile: RuntimeDefault`, all capabilities dropped, a read-only root
filesystem where feasible, a writable `/tmp` emptyDir and a `/dev/shm` emptyDir
(Chromium crashes on the default 64 MB).

## CI/CD

- Push to `main` → semantic-release cuts a versioned GitHub release from
  Conventional-Commit messages; `CHANGELOG.md` is generated, not hand-edited.
- Release published → multi-arch image build and push to GHCR
  (`linux/arm64` is the target, `linux/amd64` is also built).
- The chart is released from `charts/afas-declaraties/` in lockstep with the
  image, so `appVersion` always names a tag that exists.
- Branch names follow `<type>/<description>` with Conventional-Commit types.
