# Common mistakes / gotchas

Everything here was paid for once already. Symptom first, because the symptom is
what you will actually have in front of you.

---

## 1. `is_visible()` cannot tell you which Entra sign-in step is on screen

**Symptom.** The sign-in loop clicks `#idSIButton9` twice. The second click lands
on the *next* pane's primary button and submits an **empty password**. You get a
Dutch "voer uw wachtwoord in" style rejection, a bad-credential error, or a tick
on the account lockout counter, with nothing in the log that says why.

**Cause.** Entra is a single-page app that keeps **every pane in the DOM at
once**. On the password pane the email input is still `visibility: visible` with
a non-zero bounding box. It is merely parked in a corner, `aria-hidden` and
untabbable. So Playwright reports **both** `input[name='loginfmt']` and
`input[name='passwd']` as visible on **both** panes, and a step detector built on
`is_visible()` picks whichever branch it tests first, forever. The mirror-image
trap: waiting for a field to become *hidden* never succeeds either, because the
pane stays in the DOM. That wait times out, the loop re-detects the same step,
and clicks the shared submit again.

**Fix.** `entra._is_active()` / `_ACTIVE_JS`. An element is the one the user
would actually act on only if **all** of these hold:

- `el.offsetParent` is non-null (not `display:none`, not in a detached subtree),
- `el.closest('[aria-hidden="true"]')` is null,
- its `getBoundingClientRect()` has non-zero width and height,
- and for inputs only, `el.tabIndex >= 0`.

None of those depend on the interface language, which matters because the tenant
renders in Dutch. Transitions use `entra._await_inactive()`: wait for the
**current** control to stop being active, never for the next one to appear.

Do **not** apply the `tabIndex` rule to containers. The account picker
`#tilesHolder` is legitimately `tabIndex -1`, so a blanket rule makes the picker
undetectable and the state machine stalls in `state="waiting"` until the
deadline. That is what the `focusable=False` argument is for.

---

## 2. Entra ships `#passwordError` pre-populated but hidden

**Symptom.** Every healthy sign-in dies instantly with
`EntraLoginError: Entra rejected the sign-in: ...`, quoting text for a failure
that never happened.

**Cause.** Entra's panes carry their error containers in the markup with the text
already in them, hidden until needed. Testing for *text* alone reports a failure
on a perfectly good sign-in. `[role='alert']` is worse still: field-level hints
render there before you have typed anything.

**Fix.** `entra._raise_on_page_error()` iterates `SEL_ERROR` and `continue`s on
any handle whose `is_visible()` is false. Only a **visible** container with
non-empty text is a real error. Keep `SEL_ERROR` narrow
(`#passwordError, #usernameError, .alert-error`) and never widen it to
`[role='alert']`.

---

## 3. InSite renders TWO buttons labelled "Aanmaken"

**Symptom.** Confirming the first row files the whole declaration, so a
declaration goes in containing one day. Or the reverse: the loop thinks it is
creating the declaration and only closes a dialog.

**Cause.** The row dialog's confirm button and the page-level create button carry
**the same Dutch label**, and they do very different things:

| Element id | What it does |
|---|---|
| `Window_<n>_Actions_AntaUpdateCloseWebForm` | Confirms **one row** and closes its dialog |
| `Window_0_Actions_AntaUpdateCloseWebForm` | Creates the **whole declaration** |

**Fix.** Address both by id, never by role or label. `insite.ROW_CONFIRM_ID` and
`insite.CREATE_DRAFT_ID`. Any use of `get_by_role("button", name="Aanmaken")` is
a bug on sight.

`<n>` is a **per-page-load counter**, not a constant: it becomes 1 after the
first `Nieuw` and higher if any dialog opened before it. `insite._window_index()`
discovers it by walking open shadow roots for
`Window_(\d+)_Declaratie_HrZ50_`. Never hardcode `Window_1`. An index of `0`
after `Nieuw` means the row dialog did not open, which is a `PortalChanged`, not
something to retry into.

---

## 4. `Periode` defaults to the CURRENT month, not the booking date's month

**Symptom.** The worst kind: none. Every field looks right, the declaration files
cleanly, and August days filed on 28 August for a period that has already rolled
land in payroll period 9. You find out on a payslip.

**Cause.** A fresh row prefills `PeId` with the **current** period regardless of
`Datum boeking`, and editing the date afterwards does **not** update it.

**Fix.** Two halves, both required.

1. `insite.fill_row()` sets `PeId` explicitly on **every** row, and sets it
   *after* the date, because a later date edit can reset it.
2. `insite.verify_row()` re-reads the committed form and raises `InSiteError`
   unless `row["ref:Periode"] == str(line.period)`, with the year checked
   alongside it.

Never rely on the prefill, not even on a run that happens to be in the right
month. `ClaimLine.period` is `day.month` because this environment uses calendar
periods (confirmed against the period lookup, Januari=1 … Augustus=8); a
13-period calendar changes that one property and `store.period_for()`.

---

## 5. Each InSite field id exists TWICE, and the value lives in a different place per component

**Symptom.** Reading a field returns `None` or `""` and the form looks empty even
though the value is visibly on screen. Or a `fill()` appears to work and the
value silently reverts.

**Cause.** InSite renders controls as custom elements with an open shadow root.
`#Window_1_Declaratie_HrZ50_DaTi` matches **both** the custom-element host and
the inner `<input>` inside its shadow root. Worse, the value does not live in the
same place for every component type:

| Component | Where the value is |
|---|---|
| date / number (`DaTi`, `Qu`) | the **inner** `<input>`'s `value` **property** |
| `afas-reference` (`PeId`) | the **host**'s `value` **attribute** |

Read the wrong one and you get `None`, which reads exactly like an empty form.

**Fix.**

- Date/number: select `f"#Window_{w}_{TABLE}_{code} input"` (note the descendant
  ` input`) and use `fill()` / `input_value()`. That is `insite.set_text_field`,
  which writes, presses Tab, waits, reads back, and retries; InSite does async
  server round-trips that discard a value typed a moment earlier.
- References: click the **host** `f"#..._{code}_typeahead-base"` and pick an
  `afas-menu-item[value='<v>']`, then assert with `get_attribute("value")`. That
  is `insite.set_reference`. The inner combobox cannot be clicked, an overlay
  span legitimately intercepts pointer events, so typing into it is not an
  option.
- `insite._READ_ROW_JS` walks light DOM and open shadow roots together, keying
  plain inputs by `aria-label` and `afas-reference` hosts by `label` with a
  `ref:` prefix. That prefix is why `verify_row` looks up `ref:Periode` and plain
  `Datum boeking` / `Jaar` / `Aantal`.

`scripts/explore_insite.py` exists to dump this safely (read-only, never clicks a
submit control) when the portal moves.

---

## 6. `Aantal` is disabled on the woon-werk form

**Symptom.** A `fill()` on `Qu` times out or throws, and a retry loop burns its
attempts on a field that was never writable.

**Cause.** One row is one day's travel, both ways. The declaration profile fixes
the quantity at `1,00` and renders the field disabled. Other profiles may leave
it editable, so neither "always write" nor "never write" is correct.

**Fix.** `insite.set_or_verify_quantity()` checks `is_disabled()` **first**.

- Disabled: read the value back and raise `InSiteError` if it is not what the
  ledger intended. Filing anyway would claim a different amount than was
  recorded.
- Enabled: write it through `set_text_field` as normal.

Either way the number that will actually be filed is asserted before
`Aanmaken`. Never blind-write it and never skip the check.

---

## 7. `op` blocks forever locally when the 1Password desktop app is locked

**Symptom.** `onepassword.get_field()` hangs, then
`OnePasswordError: 'op item get ...' timed out after 30s`, with a valid service
account token and a clean environment. Nothing in `op`'s output explains it.

**Cause.** On a workstation the CLI talks to the desktop app over
`~/.config/op/op-daemon.sock` and waits on a biometric unlock that is never going
to arrive in a headless run. The service account token does not override this.

**Fix.** Set `OP_DOCKER_IMAGE=<PLACEHOLDER>` locally. `onepassword._command()`
then runs `op` **inside a container**, which has no desktop app to fall back to,
so the token is used directly. This is also exactly how the CLI runs in
production, so it is the faithful path rather than a workaround.

Two details that are not incidental:

- The token is passed **by name** (`-e OP_SERVICE_ACCOUNT_TOKEN`), never as an
  argv value. A container's argv is world-readable via `ps`.
- In the cluster `OP_DOCKER_IMAGE` stays **unset**: there is no docker socket,
  and the same 30-second timeout there means the service account token is
  missing or invalid, not that something is locked.

`get_totp()` is a second invocation on purpose (`--otp` and `--format json` are
mutually exclusive) and is called at the moment the code is typed, never up
front: the window is 30 seconds and preceding page loads eat most of it. That is
why `Credentials.totp` is a callable and not a string.

---

## 8. A degraded calendar read must NEVER be interpreted as "no office days"

**Symptom.** The most expensive failure in the system, and a completely silent
one: a whole month classified as working from home because OWA did not render,
lost its session, or changed its markup. Under-claiming looks like a quiet
success.

**Cause.** "No events found" and "the page did not load, or the label format
moved" are **indistinguishable** at the call site. Any code that returns a bare
list converts an outage into a month of wrong claims.

**Fix.** Three layers, all of them load-bearing.

1. `calendar_owa.read_week()` returns `(events, degraded)`. `degraded` is true
   when labels were harvested but **every** one failed to parse, which means the
   label format moved rather than the week being empty.
2. `classify.classify_day(..., calendar_degraded=True)` returns
   `Verdict.AMBIGUOUS` with `Reason.CALENDAR_DEGRADED`, and it does so **before**
   the booking rules, so nothing downstream can fall through to "no booking,
   therefore home".
3. `Verdict.AMBIGUOUS` maps to `DayState.NEEDS_INPUT`, which is excluded from
   `store.claimable_days()` and surfaces in `store.unresolved_days()` so a human
   is asked.

Never discard the second half of that tuple. Never wrap a calendar read in
`except: return []`. The regression test is
`tests/test_calendar_owa.py` plus `test_degraded_calendar_is_never_read_as_a_home_day`
in `tests/test_classify.py`; if you change the calendar path, that test is the
one that matters.

---

## Also worth knowing

- **`"microsoftonline.com" in url` is not a host check.** A `redirectUrl=` query
  parameter routinely carries the other side's domain, so the substring test
  reports "on the identity provider" while sitting on the application's own
  page. Use `entra.host_matches()`, which parses the hostname and accepts only
  an exact match or a subdomain.
- **Arriving on the right host is not the same as being signed in.**
  `session.wait_until_settled()` asserts the host **and** that the path is not
  one of `TRANSIENT_PATHS` (`/signin-oidc`, `/signin`, `/login`,
  `/authenticationhandler`). Dropping the second half lets a mid-handshake
  callback URL pass as authenticated; the next navigation is then bounced back to
  sign-in and the run fails somewhere unrelated.
- **The saved browser profile does not carry a session between runs** when the
  tenant suppresses "Stay signed in?". Entra issues a non-persistent cookie that
  dies with the browser process, so every run signs in afresh. That is why there
  is no session-keeper workload, and why `browser-profile/` is not a PVC.
- **`browser-profile/`, `traces/` and `artifacts/` are credentials, not caches.**
  A saved profile is a replayable, MFA-satisfied corporate session; a trace holds
  full DOM, headers and cookies. They are gitignored and must stay out of the
  Docker build context (`.dockerignore`). Never attach one to an issue.
- **Never retry a browser job.** `backoffLimit: 0` and
  `concurrencyPolicy: Forbid` everywhere. Retrying a corporate SSO login is how
  an account gets locked out, and `store.browser_lock()` yields `False` rather
  than blocking so a second job exits cleanly instead of queueing behind one
  stuck in an SSO flow.
- **`DRY_RUN` gates a path that spends real money.** It defaults to `"true"` in
  `values.yaml` and `insite.create_draft(dry_run=True)` returns without clicking.
  Flipping it is a deliberate act, never a side effect of another change.
- **This repo is public.** No employer name, InSite hostname, Entra tenant GUID,
  Slack workspace/channel/user id, OCI OCID, vault name, email address or
  employee number in any tracked file, including in a comment or a
  redacted-looking example. Use `<PLACEHOLDER>`.

## Pushing straight to main produces a release with no image

`container-image-release.yaml` calls a reusable workflow that **promotes, it does
not rebuild**: a `pull_request` build publishes `pr-<number>`, and a release then
retags that existing image as the release version. Its "Get PR Number" step walks
the release commit and its ancestors looking for the merged PR that produced them.

A commit pushed directly to `main` has no associated PR, so that lookup fails with
`Could not find merged PR for commit <sha>` and the release ends up with a chart
but no image. The HelmRelease then installs and every pod sits in
`ImagePullBackOff` against a tag that was never built — which reads like a registry
or credentials problem rather than a missing upstream build.

Every change destined for a release goes through a PR, including one-line CI fixes.

## v1.0.29 of the shared docker-bake reusable workflow fails at startup

Calling `docker-bake-ghcr.yaml@v1.0.29` produced `startup_failure` on both
`release` and `workflow_dispatch`, with no job created and nothing in the logs.
actionlint passed, the SHA resolved, the source repo is public and Actions
permissions matched the siblings. Pinning to v1.0.24 — the version
fortivpn-gateway runs — starts normally. Do not "upgrade" this pin without
checking a run actually starts.

Dependabot moved it anyway, to v1.15.0, where `docker-bake-ghcr.yaml` no longer
exists. A run like that has zero jobs and no log, and `gh pr checks` does not list
it, so the PR looks green. No image built for seven weeks and nothing noticed,
because no release was cut in that time. The next one would have had no image.
`dependabot.yml` now ignores that repo. Moving up means porting to the workflows
that replaced it, with a run to prove it. Check `gh run list --workflow "Build
Container Image"` before merging, not only the PR's checks.

## A Chromium selftest in the Dockerfile breaks the cross-built leg

The image build launches Chromium once to prove the browser works, so a broken
image fails the build rather than the 23:30 CronJob. That check cannot run on the
emulated leg: buildx builds the non-native platform under QEMU, which does not
implement `ptrace`, and Chromium dies during sandbox setup with
`ptrace: Function not implemented`. It looks like a broken image and is not.

It passes locally because a native build is not emulated — on Apple Silicon the
arm64 leg is native and only amd64 is emulated, which is the opposite of CI.
The step is guarded on `TARGETPLATFORM != BUILDPLATFORM` and skips with a printed
reason; the native leg still runs it on every build.

## The release promotes `<image>-<bake target>` unless bake_target is named

The shared docker-bake workflow supports repos that build several images from one
bake file (fortivpn-gateway builds cookie/vpn/bgp). Its promote step lists the
bake targets and, for any target whose name differs from the `bake_target` input,
promotes `<image_name>-<target>`.

`bake_target` defaults to `default`. Our group `default` resolves to the single
target `app`, so the release looked for `afas-declaraties-app:pr-N` while the PR
build had pushed `afas-declaraties:pr-N`. The chart published, the image did not,
and the failure surfaced only at release time — long after the PR was green.

Passing `bake_target: app` makes the target match and the single-image branch is
taken. Renaming the bake target would work too; naming the input is clearer.

## OWA week navigation: the nav button's label is not the current week

Two traps, both silent, both of which classify real office days as home.

1. **Deep links do not work.** `/calendar/view/workweek?startdate=YYYY-MM-DD`
   and `/calendar/view/workweek/YYYY/MM/DD` both render the CURRENT week, with
   no error and a full set of labels. Navigate by clicking the calendar's own
   `Previous week` / `Next week` buttons instead.
2. **`Go to previous week` carries the previous week's date range** in its
   aria-label, and it appears in the DOM before the header. Matching the first
   date range on the page therefore reads a week that is not on screen, so every
   navigation decision is made against a baseline one week out. Match the header
   specifically -- it is the label containing "Jump to a specific date".

Symptom: weeks other than the current one return zero events while reporting
`degraded=False`, so the classifier calls every day home. Regression test in
`tests/test_calendar_week.py`.

## A blocking `start()` means the heartbeat is only ever written by traffic

`SocketModeHandler.start()` is `connect()` followed by a forever-wait. Calling
it leaves no room to refresh anything, so the only heartbeats slackd wrote were
the ones its interaction handlers wrote — and a week with no approvals delivers
no interactions. The liveness probe (file older than 180s) then killed a
perfectly healthy, connected process on a fixed cadence: `initialDelay 30` +
`180` stale + `3 × 30` failures = **exactly 5 minutes**, forever.

Symptom: `exitCode: 137`, `reason: Error` (not `OOMKilled`), `lastState`
`startedAt`/`finishedAt` exactly 5 minutes apart, restart count in the hundreds,
and logs that end on `⚡️ Bolt app is running!` every time. `READY 1/1` the whole
while, because readiness recovers on each restart.

Use `handler.connect()` and own the loop. Refresh on a timer gated on
`handler.client.is_connected()`, which keeps the property the probe was built
for: a socket that silently dies stops the refresh and earns its restart.

## A CronJob for every command the pipeline needs, or the last mile is manual

`submit` existed as a CLI command, was documented, was tested — and nothing in
the cluster ever ran it. The chart shipped `classify`, `digest` and `build`, so
an approval click updated Postgres and stopped there.

Check the set of `jobs:` in `values.yaml` against the set of subcommands in
`cli.py` whenever either changes. A command with no caller looks identical to a
working feature from inside the repo.

And when adding that job: **do not derive the period from the date.** `build`
runs on the 28th for the *previous* month, so any submit landing on or after
the 1st would compute a month later than the one approved. Read the period off
the approved row. Regression tests in `tests/test_submit_selection.py`.

## An `ON CONFLICT DO UPDATE` only sets the columns it lists

`apply_override` wrote `verdict` in its `VALUES` but never listed it in the
`DO UPDATE SET` clause. A brand-new row therefore got the right verdict and an
existing row kept the classifier's old one, so a day corrected from office to
home read `verdict=office, claim_type=home` — claiming one thing and saying
another.

Nothing gated on `verdict` (`claimable_days`, `weekly_digest` and `classify`'s
`previous_verdict` all go off `claim_type`), so no claim was wrong. That is luck,
not design: the ledger is the system of record and a self-contradicting row in it
is a defect on its own terms.

Two habits from this. When adding a column to an upsert's `VALUES`, add it to
`DO UPDATE SET` in the same edit. And when a write path and a read path disagree
about which column is authoritative, say so in the schema rather than leaving it
to whoever reads the table next.

The same function also wrote the literal `'human'` as a verdict, which is not a
member of the `Verdict` enum. It never blew up because nothing parses the column
back. Derive it from the claim type instead; `human_override` in `reasons` is
what records that a person decided.

## Every handler that writes needs the replay guard, not just the scary one

`handle_approve` called `once()`; `handle_corrections` did not, so
`slack_interaction` stayed empty after a real correction and a Socket Mode replay
would have applied the overrides twice. Idempotent in value, but it doubles the
`overridden_at` and posts a second summary to the channel.

Socket Mode replays on reconnect, and the socket does reconnect — Slack rotates
the endpoint roughly every five hours, visible in slackd's own logs. Treat replay
as routine, not exceptional.

While there: ack a `view_submission` BEFORE the database work, not after. Slack
drops an unacked submission after three seconds and redelivers it, and a slow
database is exactly when that fires.

## The M365 connector answers in blocks, and joining them corrupts the JSON

`tools/call` returns `content` as a **list of text blocks**, one JSON object
each: an optional `searchInfo` header, one block per result, a pagination
footer, and -- when a scan was cut short -- a block of plain prose. The obvious
client does `''.join(b['text'] for b in content)`, which produces
`}{`-concatenated JSON that `json.loads` refuses, and silently swallows the
prose note that was the only statement that the answer is partial.

Parse each block separately and classify it. `m365_mcp.call()` does that and
records a block it cannot place as a **note**, which marks the search
incomplete, which `read_range` turns into `degraded`. Neither extreme is right:
skipping it quietly would look exactly like a week with no desk bookings, which
is COMMON_MISTAKES #8 wearing a different hat, and raising would let one new
metadata block Anthropic adds -- at an endpoint they own and do not document --
break every classification run outright.

The footer is also not one fixed shape. A single-page answer ends
`{"totalResultCount": 12}` with no `nextOffset` at all; a paged one ends
`{"moreResults": true, "nextOffset": 25, "totalResultCount": 60}`; the chat
search's footer carries no total. Treat "any pagination key present" as the
footer, not "all of them".

## An all-day calendar event is a half-open range, and leave arrives as ONE of them

**Symptom.** Monday is correctly marked absent and Tuesday to Friday of the same
holiday are classified `home` and claimed. Nothing errors, and the calendar
plainly shows the leave.

**Cause.** Graph returns a week of leave as a **single** event running from
Monday 00:00 to **Saturday** 00:00, `isAllDay: true`. Reading `start.date()`
gives one day. The end is exclusive, so the days covered are
`start .. end - 1 day`, and they have to be expanded.

`calendar_mcp.parse_event()` returns a **list** for exactly this reason, and the
regression test is `test_multi_day_leave_covers_every_day_it_spans`.

Two more traps in the same place:

- **Never convert an all-day event's bounds through a time zone.** A day-long
  entry on the 3rd is on the 3rd everywhere; converting its midnight bounds out
  of a zone behind the local one moves it to the 2nd and misdates every desk
  booking. Read the date part literally. Timed events *are* converted, because
  22:30 UTC really is tomorrow in Amsterdam.
- **The zone is named the Windows way, not the IANA way.** `ZoneInfo("W. Europe
  Standard Time")` raises. There is a small map; an unknown name falls back to
  UTC with a warning, which is proportionate because only timed events consult
  it at all.

## The connector filters on the event's own start, so a running absence is invisible

**Symptom.** A fortnight of leave that began the week before the classification
window produces no events inside it, so those days read "no booking, therefore
home" and the working-from-home allowance is claimed for a holiday.

**Cause.** `afterDateTime` filters on when the event *starts*. An event that
started before the window and is still running is simply not returned.

**Fix.** `calendar_mcp.LOOKBACK_DAYS` reaches the query a month further back
than the window being classified, and the extra days are clipped off after
expansion. The cost is one more page of results; the alternative is
over-claiming, which is the one direction this system may not fail in.

## A mounted Secret is read-only, and Entra rotates the refresh token on use

Every refresh returns a **new** refresh token. Writing it back into a Secret
mounted at `/etc/...` fails with `EROFS`, so a naive client keeps replaying the
original one until it ages out -- at which point the nightly job starts failing
with an auth error weeks after the change that caused it.

`m365_mcp` therefore seeds a writable cache under `/tmp` from `M365_TOKEN_JSON`
(or `M365_TOKEN_SEED`) at the start of each run and lets the rotation land
there. Losing that rotation when the pod exits is fine -- the seed keeps its own
90-day sliding window -- but the token must be *used* inside that window or it
dies, and re-minting it needs an interactive device-code sign-in.

Related: `token()` will **not** start a device-code flow unless asked to. A
device code blocks until a human types it into a browser, and a CronJob has no
human; failing immediately with the login command in the message beats a job
that hangs until its `activeDeadlineSeconds`.

## Live approval could never have worked, and dry run hid it

**Symptom.** Under `DRY_RUN=false`, every Approve is refused because the digest
changed, and nothing is ever filed. Under `DRY_RUN=true` everything looks fine.

**Cause.** `build` marked the days `drafted`, and `claimable_days` left `drafted`
out. The digest re-derived at approval and at submit therefore covered no days,
and never matched the one on the card. Dry run does not mark the days, so the
path that gets exercised was the one path this bug could not reach.

**Fix.** `claimable_days` includes `drafted`. The test that catches it is
`tests/test_lifecycle_pg.py`, which walks build, approve and submit against a real
Postgres. The fakes in `test_submit_selection.py` could not: they answer whatever
the test told them to.

## `drafted` on a submission means rehearsed, not that a draft exists

A dry run enters and verifies the rows, leaves them unsaved, and records the
submission as `drafted`. AFAS holds nothing. A live run has to do the work for
real, which is why `_plan_build` skips a `drafted` row only under `DRY_RUN`.
Reading that state as "the draft is there" is how a month goes unfiled the day
`DRY_RUN` is flipped.

## A monthly CronJob's failure alert takes 62 days to fire

`KubeCronJobNotSucceeding` fires when a CronJob has had no success within twice
its schedule interval, and a day-of-month schedule counts as 31 days.
`KubeJobFailed` excludes jobs a CronJob owns. So a job on `0 6 28 * *` can fail two
months running and nobody hears about it.

`build` therefore runs daily and opens no browser unless a month has work, which
brings the cluster alert down to two days and gives a failed month a next attempt.
The app's own Slack alert covers the rest. Do not move it back to a monthly
schedule, and do not add a retry to shorten the gap: a retried SSO login is how an
account gets locked out.

## AFAS holds claims the ledger never saw

Claims can be filed by hand, before the ledger began or alongside it. The first
approval card ever posted offered two days that were already approved in AFAS, and
approving it live would have claimed them twice.

Read *Mijn declaraties* before writing anything and treat what it says as the
truth. That is `readback.position()`. A click that did not raise is not evidence
either: `submitted` is only written once the list shows the lines with the
manager.

Same reasoning for backfills: a desk booking is not proof of presence when
bookings are made automatically, so old days are for a human to decide. Never file
them from the calendar alone.

## The period menu does not list every period

**Symptom.** `build` crashes about 70 seconds in with `PortalChanged: could not
select PeId=9 via #Window_1_Declaratie_HrZ50_PeId_typeahead-base`. One month the
commute form passes and the thuiswerk form fails for the same period, and the next
month the commute form fails too. Nobody changed anything, and it looks like the
portal moved.

**Cause.** The menu shows the employee's recent picks plus the first rows of the
lookup, six in all. A period already picked by hand on that form is in the DOM. Any
other period past March is not, so `afas-menu-item[value='9']` waits for something
that will never appear.

**Fix.** `set_reference` checks whether the item exists and otherwise types the
value. Opening the menu focuses its search box, and the search matches the period
number, so this holds in any interface language.

## The woon-werk and thuiswerk forms disagree about what one looks like

The woon-werk form holds `1,00` in a disabled field. The thuiswerk form holds `1`.
Comparing them as strings raises on a correct row, so `verify_row` and
`set_or_verify_quantity` compare numbers through `same_quantity`. A quantity that
already holds the right count is left alone.

## The claim list shows an open draft twice on one page

An open draft has a grid of its own above the main one, and its lines are in the
main grid as well. Counting lines per page therefore doubles every draft line, and
treating two lines on a day as a conflict then sends every draft to a human. Count
per grid: `parse_lines` splits on the header, so a line held twice by one grid is a
real duplicate and a line in both grids is one line.

## An enabled pager that moved nothing is not the end of the list

On the real list the last page's *Volgende* is disabled. Reading "the text did not
change" as the end instead lets one slow render pass part of the list off as all of
it, and a filed day on the unread page looks absent and gets claimed twice. So
`_turn_page` waits for the page to change and settle, and raises if it never does.

## A click on a dry card must not count once live

`approve_submission` used to accept `drafted`, which is the state a dry run leaves.
A rehearsal's card stays in Slack with its buttons after `DRY_RUN` is flipped, its
footer says nothing will be filed, and clicking it approved a real filing if the
lines matched. Dry cards now offer corrections only, and `drafted` cannot be
approved or rejected, which also covers cards already posted.

## A failed calendar read must not erase what a good one decided

**Symptom.** Days the digest showed as home or commute are back on `needs_input`
with `calendar_degraded`, and the oldest of them never recovers. The claim card
lists them under *Niet meegenomen* and nothing else says why.

**Cause.** Classify rewrites its whole 9-day window every night, and one degraded
read turned every day in it into a question. The next good read healed the window,
but the oldest day had already dropped out of it and stayed a question until a
human answered.

**Fix.** `cmd_classify` skips a day that already has a row when the read is
degraded. A new day still becomes a question, and the next nine nights can settle
it. A read that failed is not evidence, so a day that was decided stays decided.
