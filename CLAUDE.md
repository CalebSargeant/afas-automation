# CLAUDE.md

`afas-declaraties`: classifies each working day as office or working-from-home
from the Outlook Web calendar, keeps a claim ledger in Postgres, then drives the
AFAS InSite *verzameldeclaratie* form with Playwright to file the monthly
travel/WFH claim. Approvals and corrections happen in Slack over Socket Mode.
Package `src/afas_declaraties/`, entrypoint
`python -m afas_declaraties.cli <classify|digest|build|slackd|override>`.
Ships as `ghcr.io/calebsargeant/afas-declaraties` (arm64) and deploys via the
in-repo Helm chart at `charts/afas-declaraties/`.

## Commands
See @.claude/QUICK_START.md. Tests are `python -m pytest`; `test_classify.py` and
`test_calendar_owa.py` need no browser, no network and no database. `cli submit`
is LIVE: it clicks the irreversible *Declaratie insturen* action whenever
`DRY_RUN=false`, gated by a Slack approval from an allow-listed user and a
`lines_digest` re-derived from the ledger. That click files real claims on the
live portal.

## Architecture
@.claude/ARCHITECTURE_MAP.md

## Gotchas
@.claude/COMMON_MISTAKES.md -- read this before touching `entra.py`,
`insite.py` or `calendar_owa.py`. Every entry is a bug that has already been
paid for once.

## Non-negotiables
- **This repo is public.** Never write an employer name, the InSite hostname, an
  Entra tenant GUID, Slack workspace/channel/user ids, OCI OCIDs, vault names,
  email addresses or employee numbers into a tracked file. Use `<PLACEHOLDER>`.
  That includes comments, test fixtures and "redacted-looking" examples.
- **`DRY_RUN` defaults to `"true"`.** It gates a path that spends real money.
- **Never retry a browser job.** `backoffLimit: 0`, `concurrencyPolicy: Forbid`.
  A retried corporate SSO login risks account lockout.
- **Claim less, never more.** An omitted day costs a couple of euros; a wrong day
  is a false expense claim. Ambiguity becomes a Slack question, not a guess.
- `browser-profile/`, `traces/`, `artifacts/` are credential-grade. Gitignored,
  `.dockerignore`d, never attached anywhere.

## Finding code
- `.claude/ARCHITECTURE_MAP.md` first, then targeted line-range reads.
- Load `.claude/decisions/` and `.claude/sessions/` only when the task relates to
  them, never by default.
- `AGENTS.md` and `./docs` hold the fuller prose.

## [tooling]
- Prefer targeted reads over whole files.
- With grep/find/glob, return matching paths and matched lines only.
- Pipe flood-prone output (logs, `helm template`, page dumps) through
  `head`/`tail`/`grep` or redirect to `.claude/last_output.txt` and read ranges.
- After a successful write/edit, trust it; don't re-read to "verify".

## [maintenance]
- Bug that took >1h to solve: append it to `.claude/COMMON_MISTAKES.md`.
- Architectural decision: run `/adr`.
- Public behaviour/API/config/setup changed: run `/update-docs`.
- Keep this file under ~500 tokens; push detail into on-demand `.claude/` files.
