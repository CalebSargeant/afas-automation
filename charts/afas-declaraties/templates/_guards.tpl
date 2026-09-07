{{/*
Preflight checks.

Every one of these describes a failure that is slow and misleading to diagnose
once it is running in a cluster: a pod in ImagePullBackOff that looks like a
registry credentials problem, a CrashLoopBackOff whose log says "DATABASE_URL
is required" three seconds after every restart, or — worst of all — a live run
that files real expense claims because a value defaulted the wrong way.
`fail` turns each into an immediate, readable `helm template` error.

Included once from NOTES.txt, so they run on template, install and upgrade.

Cross-field rules live here rather than in values.schema.json because JSON
Schema cannot express them with a message worth reading.
*/}}
{{- define "afas.guards" -}}

{{/*
---------------------------------------------------------------------------
appVersion doubles as the default image tag.

The release workflow publishes `v<version>` tags. An appVersion of "0.1.0"
therefore resolves to ghcr.io/calebsargeant/afas-declaraties:0.1.0 — a tag that
does not exist — and every pod sits in ImagePullBackOff. The event says
"manifest unknown", which reads exactly like a private-registry auth failure
and sends you to check pull secrets that were never the problem.
--------------------------------------------------------------------------- */}}
{{- if not (hasPrefix "v" .Chart.AppVersion) }}
{{- fail (printf "Chart.yaml appVersion is %q but must start with \"v\" (e.g. \"v0.1.0\"). appVersion is the default image tag and the release workflow publishes v-prefixed tags, so without it every pod lands in ImagePullBackOff with \"manifest unknown\" — which looks like a registry credentials problem and is not." .Chart.AppVersion) }}
{{- end }}

{{- $opaqueSecret := ne (default "" .Values.existingSecret) "" }}
{{- $declared := splitList "," (include "afas.declaredEnvKeys" .) }}

{{/*
---------------------------------------------------------------------------
Two writers of one Secret.

Whichever reconciles last wins, and the symptom is a credential that
intermittently reverts — the kind of bug that gets blamed on the vault.
--------------------------------------------------------------------------- */}}
{{- if and $opaqueSecret .Values.externalSecret.enabled }}
{{- fail "existingSecret and externalSecret.enabled are mutually exclusive: both write the pods' credential Secret, whichever reconciles last wins, and the symptom is a credential that intermittently reverts. Pick one." }}
{{- end }}

{{- if and .Values.externalSecret.enabled (not .Values.externalSecret.secretStore.name) }}
{{- fail "externalSecret.enabled is true but externalSecret.secretStore.name is empty. ESO cannot resolve a store it cannot name, the Secret is never created, and every pod stays in CreateContainerConfigError." }}
{{- end }}

{{/*
---------------------------------------------------------------------------
Database.

The app reads one DATABASE_URL and it carries the password, so it is a
credential: it comes from a Secret, never from a values file. Unset, config
raises at import and every workload CrashLoopBackOffs identically — including
the CronJobs, which fail silently at 23:30 with nobody watching.
--------------------------------------------------------------------------- */}}
{{- if not (or .Values.database.existingSecret $opaqueSecret (has "DATABASE_URL" $declared)) }}
{{- fail "No source for DATABASE_URL. The app reads a single DSN and it contains the password, so it must come from a Secret: set database.existingSecret (+ database.existingSecretKey), or add DATABASE_URL to externalSecret.data pointing at the vault entry. Never put a DSN in a values file — this chart lives in a public repository." }}
{{- end }}

{{- if and .Values.database.existingSecret (not .Values.database.existingSecretKey) }}
{{- fail "database.existingSecret is set but database.existingSecretKey is empty; the secretKeyRef would name no key." }}
{{- end }}

{{/*
---------------------------------------------------------------------------
INSITE_HOST.

The AFAS InSite hostname identifies the employer, so it is deliberately absent
from this chart, from ci/default-values.yaml and from any HelmRelease in a
public repository. It only ever arrives from the vault. Without it there is no
portal to drive and config refuses to build.
--------------------------------------------------------------------------- */}}
{{- if not (or $opaqueSecret (has "INSITE_HOST" $declared)) }}
{{- fail "No source for INSITE_HOST. Add it to externalSecret.data (e.g. INSITE_HOST: {key: afas-declaraties-insite-host}) or supply an existingSecret that carries it. Do NOT add the hostname to a values file: it names the employer and this repository is public." }}
{{- end }}

{{/*
---------------------------------------------------------------------------
Calendar source.

An unrecognised value would be refused by config.py at runtime, which means at
23:30 with nobody watching. And "mcp" with no token is worse than a crash: the
connector call fails, the reader reports the window degraded, and every day in
it lands in needs_input — a month of Slack questions caused by a missing
secret, with nothing in the message that says so.
--------------------------------------------------------------------------- */}}
{{- $calendarSource := .Values.config.calendarSource | toString | lower }}
{{- if not (has $calendarSource (list "owa" "mcp")) }}
{{- fail (printf "config.calendarSource must be \"owa\" or \"mcp\", got %q. config.py refuses anything else, so every workload would exit 2 at its scheduled time." .Values.config.calendarSource) }}
{{- end }}
{{- if eq $calendarSource "mcp" }}
{{- if not (or $opaqueSecret (has "M365_TOKEN_JSON" $declared) (has "M365_TOKEN_SEED" $declared)) }}
{{- fail "config.calendarSource is \"mcp\" but nothing supplies M365_TOKEN_JSON. Run `python -m afas_declaraties.m365_mcp login` once, put the resulting JSON in the vault, and add M365_TOKEN_JSON to externalSecret.data. Without it the calendar read fails, every day in the window is recorded needs_input, and the only clue is a stack trace in a CronJob log." }}
{{- end }}
{{- end }}

{{/*
---------------------------------------------------------------------------
The money guard.

DRY_RUN=false means `build` files a real verzameldeclaratie. The approver
allowlist is the only thing standing between "someone can see the channel" and
"someone can spend money", because a Slack button is clickable by every member
of the channel it was posted in. An empty allowlist with DRY_RUN=false is a
misconfiguration, not a permissive default; the app refuses to start on it, and
this refuses to render it, which is several hours earlier.
--------------------------------------------------------------------------- */}}
{{- $dryRun := .Values.config.dryRun | toString | lower }}
{{- if not (has $dryRun (list "true" "false")) }}
{{- fail (printf "config.dryRun must be \"true\" or \"false\", got %q. Anything unrecognised would be read as false by some parsers, and false spends money." .Values.config.dryRun) }}
{{- end }}
{{- if eq $dryRun "false" }}
{{- if not (or $opaqueSecret (has "SLACK_APPROVER_IDS" $declared)) }}
{{- fail "config.dryRun is \"false\" (real expense claims will be submitted) but nothing supplies SLACK_APPROVER_IDS. With an empty allowlist any member of the Slack channel can approve a submission that spends real money. Add SLACK_APPROVER_IDS to externalSecret.data, or set config.dryRun back to \"true\"." }}
{{- end }}
{{- if not .Values.slackd.enabled }}
{{- fail "config.dryRun is \"false\" but slackd.enabled is false. Nothing would be listening for the approval click, so claims would be built and never approved — while the submit path is live. Enable slackd, or keep dryRun \"true\"." }}
{{- end }}
{{- end }}

{{/*
---------------------------------------------------------------------------
Everything else ESO must resolve.

ESO applies `data` all-or-nothing: one missing or non-ACTIVE vault entry leaves
the entire Secret absent and every pod down, with an error that names the store
rather than the key.
--------------------------------------------------------------------------- */}}
{{- if and .Values.externalSecret.enabled (not $opaqueSecret) }}
{{- $missing := list }}
{{- range .Values.externalSecret.requiredKeys }}
{{- if not (has . $declared) }}
{{- $missing = append $missing . }}
{{- end }}
{{- end }}
{{- if $missing }}
{{- fail (printf "externalSecret.data is missing required keys: %s. Add each as ENV_VAR: {key: <vault entry name>}, or drop it from externalSecret.requiredKeys if the workload that needs it is disabled. ESO resolves the list all-or-nothing, so a partially wired map is not a partially working deployment — it is no Secret at all." (join ", " $missing)) }}
{{- end }}
{{- end }}

{{/*
---------------------------------------------------------------------------
CronJob sanity.
--------------------------------------------------------------------------- */}}
{{- range $name, $job := .Values.jobs }}
{{- if $job.enabled }}
{{- if not $job.schedule }}
{{- fail (printf "jobs.%s is enabled with an empty schedule. The CronJob would be rejected by the API server with a validation error that quotes the empty string and nothing else." $name) }}
{{- end }}
{{- if ne (int $job.backoffLimit) 0 }}
{{- if $job.browser }}
{{- fail (printf "jobs.%s drives a browser and has backoffLimit %v. A retry re-drives the corporate SSO login, and repeated failed logins lock the account. Leave it at 0 and let the next scheduled run be the retry." $name $job.backoffLimit) }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}

{{- end }}
