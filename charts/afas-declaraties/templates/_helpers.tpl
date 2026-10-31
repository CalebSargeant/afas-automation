{{/* Standard name helpers. */}}

{{- define "afas.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "afas.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "afas.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "afas.labels" -}}
helm.sh/chart: {{ include "afas.chart" . }}
{{ include "afas.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "afas.selectorLabels" -}}
app.kubernetes.io/name: {{ include "afas.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "afas.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "afas.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
The image reference. A digest wins over a tag when set — tags are mutable and
production should not be one `docker push` away from running different code.
An empty tag falls back to the chart's appVersion, which is why _guards.tpl
insists appVersion keeps its leading "v": the release workflow publishes
`v<version>` tags, and a bare "0.1.0" resolves to a tag that does not exist.
*/}}
{{- define "afas.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}
{{- end -}}

{{/*
The Secret every workload reads credentials from. Exactly one writer: the
ExternalSecret, or something outside the release. Empty when neither exists,
which only survives _guards.tpl if every required key has another source.
*/}}
{{- define "afas.secretName" -}}
{{- if .Values.existingSecret -}}
{{- .Values.existingSecret -}}
{{- else if .Values.externalSecret.enabled -}}
{{- include "afas.fullname" . -}}
{{- end -}}
{{- end -}}

{{/*
envFrom shared by slackd and all the CronJobs — the same settings drive every
workload, so drift between them would be a bug, not a feature.

Order matters: the ConfigMap first, the Secret second. A key present in both
resolves to the Secret's value, which is what you want when a tenant-specific
setting graduates from "config" to "must not be in git".
*/}}
{{- define "afas.envFrom" -}}
- configMapRef:
    name: {{ include "afas.fullname" . }}
{{- with (include "afas.secretName" .) }}
- secretRef:
    name: {{ . }}
{{- end }}
{{- with .Values.extraEnvFrom }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/*
Individual env vars, as list items so a caller can append. DATABASE_URL is the
only one the chart projects by hand, and only when it lives in a Secret the
release does not own; otherwise it arrives through envFrom above.
*/}}
{{- define "afas.env" -}}
{{- if .Values.database.existingSecret }}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.existingSecret }}
      key: {{ .Values.database.existingSecretKey }}
{{- end }}
{{- with .Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/*
Volumes and mounts. Call with (dict "root" $ "browser" true|false).

/tmp is always present: the root filesystem is read-only and both Playwright
and the heartbeat file need somewhere to write. /dev/shm is only for the
browser jobs — Chromium's default 64 MB is not enough and it dies with a bare
"Target closed" that names nothing.
*/}}
{{- define "afas.volumes" -}}
{{- $root := .root -}}
- name: tmp
  emptyDir:
    sizeLimit: {{ $root.Values.browser.tmpSize }}
{{- if .browser }}
- name: dshm
  emptyDir:
    medium: Memory
    sizeLimit: {{ $root.Values.browser.shmSize }}
{{- end }}
{{- with $root.Values.extraVolumes }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "afas.volumeMounts" -}}
{{- $root := .root -}}
- name: tmp
  mountPath: /tmp
{{- if .browser }}
- name: dshm
  mountPath: /dev/shm
{{- end }}
{{- with $root.Values.extraVolumeMounts }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/*
Every env var name that has a declared source: the keys of externalSecret.data
plus anything named in extraEnv. Used by the guards to tell "you forgot to wire
this" apart from "this is deliberately unset".
*/}}
{{- define "afas.declaredEnvKeys" -}}
{{- $keys := list -}}
{{- if .Values.externalSecret.enabled -}}
{{- $keys = concat $keys (keys .Values.externalSecret.data) -}}
{{- end -}}
{{- range .Values.extraEnv -}}
{{- $keys = append $keys .name -}}
{{- end -}}
{{- $keys | join "," -}}
{{- end -}}

{{/*
The heartbeat check, shared by slackd's liveness and readiness probes.

slackd serves no HTTP: Socket Mode dials out, so there is no port to probe and
no Service to route to. Instead the process rewrites a file every 30
seconds for as long as its socket is up, and the probe asserts the file is
recent. A silently disconnected socket — Bolt believing it is connected while
Slack has stopped delivering — looks identical to a healthy process from the
outside, and this is what tells them apart.

The refresh is on a timer and not on inbound Slack traffic: a quiet week
delivers no events, and a probe that waits for one restarts a process that is
doing its job.

python rather than shell: `stat` takes different flags on busybox and GNU
coreutils, and the probe must not depend on which base image the app is built
from. python is by definition present.
*/}}
{{- define "afas.heartbeatCheck" -}}
exec:
  command:
    - python
    - -c
    - >-
      import os, sys, time;
      p = {{ .Values.slackd.heartbeatFile | quote }};
      sys.exit(0 if os.path.exists(p) and time.time() - os.stat(p).st_mtime < {{ .Values.slackd.heartbeatMaxAgeSeconds }} else 1)
{{- end -}}
