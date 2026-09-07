# AFAS Declaraties

Automated monthly travel and working-from-home expense claims for **AFAS InSite**.

The system reads the work calendar -- over the Microsoft 365 MCP connector, or
by driving Outlook Web -- decides day by day whether
that was a commute or a day worked at home, records every decision with its
evidence in a Postgres ledger, and then drives the InSite
*verzameldeclaratie* form with Playwright to file the claim. Anything ambiguous
becomes a question in Slack, and the irreversible submission happens only after a
human approves it.

!!! warning "Public repository"
    This repo contains **no employer identifiers**. No hostnames, tenant ids,
    channel ids, vault names, email addresses or employee numbers appear in any
    tracked file. Every example value is a `<PLACEHOLDER>`; the real ones live in
    OCI Vault and cluster secrets.

## The shape of it

```
work calendar ->  classify  ->  Postgres ledger  ->  Slack  ->  InSite  ->  submitted
                     ^                                 |
                     +---------- human override -------+
```

One image, five commands, five workloads:

| Command | Runs as | When (Europe/Amsterdam) |
|---|---|---|
| `classify` | CronJob | nightly 23:30 |
| `digest` | CronJob | Fridays 16:00 |
| `build` | CronJob | 28th of the month 06:00 |
| `submit` | CronJob | daily 07:00 |
| `slackd` | Deployment (1 replica) | always on |
| `override` | manual | on demand |

The final *Declaratie insturen* click **is implemented** and runs when `DRY_RUN=false`. It has never been executed against the live portal.
See [Architecture](architecture.md#build).

## Design principles

**The ledger is the system of record, not AFAS.** A portal change, a rejected
line or a lost session must never lose the knowledge of what was worked where.
AFAS is an output format.

**Claim less, never more.** An omitted day costs a couple of euros. A wrong day
is a false expense claim. Every rule is asymmetric in that direction: an office
day needs positive evidence, home is the residual, and a contradiction is a
question rather than a guess.

**A day is either a commute or a home day, never both.** Art. 31a lid 12
Wet LB 1964 makes the two exemptions mutually exclusive per day, so the ledger
enforces it structurally: `claim_day` is keyed by date and physically cannot hold
both.

**Verify every write.** Both Entra and InSite silently discard values typed a
moment earlier. Every field is read back before the next step, and every row is
re-read and asserted before it is committed.

**Never retry a browser job.** A retried corporate SSO login risks account
lockout, so browser workloads run with `backoffLimit: 0` and
`concurrencyPolicy: Forbid`, and a Postgres advisory lock makes a second job exit
cleanly rather than queue behind one stuck in an SSO flow.

## Documentation

- **[Architecture](architecture.md)** - modules, the classification rules, the
  ledger and its state machine, and the portal quirks that shaped the code.
- **[Setup & Deployment](setup.md)** - configuration, local runs, the Helm chart
  and the CI pipeline.

!!! note "Agent context"
    Terse machine-facing context lives in `CLAUDE.md` and `.claude/*.md`.
    `.claude/COMMON_MISTAKES.md` in particular is required reading before
    touching the browser code. These human docs are the fuller, published
    surface.
