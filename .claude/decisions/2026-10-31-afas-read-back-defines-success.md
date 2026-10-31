# AFAS read-back defines success, and build runs daily

Status: accepted

## Context

The ledger was the only record of what had been claimed, and `submitted` meant a click had not raised. Claims filed by hand were invisible to the app, so the first approval card offered days that were already approved in AFAS. `build` ran once a month with `backoffLimit: 0`, so a crash lost the month. Two builds crashed. Nobody was told, because the cluster's alert on a monthly CronJob takes 62 days to fire and `KubeJobFailed` excludes CronJob-owned jobs.

## Options considered

1. **Keep monthly, add Slack alerts.** Failures get seen, but a human has to re-run each one.
2. **Retry the job.** Quickest recovery, but a retried corporate SSO login risks locking the account.
3. **Run `build` daily and make it idempotent, reading AFAS before and after each write.** More portal reads, and depends on one list page staying parseable.

## Decision

Option 3.

- AFAS outranks the ledger. A day AFAS shows as filed is recorded as filed and not offered again.
- `submitted` is written only when *Mijn declaraties* shows the lines with the manager.
- A click whose outcome is unknown becomes `needs_reconciliation`. The next run reads AFAS and never clicks again to find out.
- `build` runs daily at 06:00 and opens no browser unless a month has work. `backoffLimit: 0` and `concurrencyPolicy: Forbid` stay.
- Failed, skipped and unconfirmed runs post straight to Slack, without going through the database.

## Rationale

A false expense claim is the one failure this system may not have, and only AFAS can say what it holds. A daily schedule gives a failed month a next attempt one sign-in at a time, which is not a retry loop, and brings the cluster alert down to two days.

## Consequences

- `CLAIMS_PAGE_PATH` is a new required setting for `build` and `submit`.
- A change to the list page stops `build` and `submit` with `PortalChanged`, and an alert. Nothing is filed while it cannot be read.
- A second open draft of one claim type blocks that type, because the task list cannot tell drafts apart. A human has to send or delete it.
- Backfills go through the same path as any other month. Old days are entered by a human as overrides and never inferred from bookings.
