{{/* Resource names: <release>-<component>, e.g. gk-backend. */}}
{{- define "gatekeeper.name" -}}
{{- printf "%s-%s" .root.Release.Name .component | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{/* Labels on every object. */}}
{{- define "gatekeeper.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end }}

{{/* Selector labels for one component. Never change these: selectors are immutable. */}}
{{- define "gatekeeper.selectorLabels" -}}
app.kubernetes.io/name: {{ .root.Chart.Name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Labels for a component's PODS: no version or chart label. Those change with every
release, and a changed pod template restarts the pods; a release must only restart
what it actually changes (Keycloak, say, keeps running through an app upgrade).
*/}}
{{- define "gatekeeper.podLabels" -}}
{{ include "gatekeeper.selectorLabels" . }}
app.kubernetes.io/part-of: gatekeeper
{{- end }}

{{/* All labels for one component's objects (not its pod templates). */}}
{{- define "gatekeeper.componentLabels" -}}
{{ include "gatekeeper.labels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/* registry/repository:tag for backend or frontend. The tag is required: a release is pinned. */}}
{{- define "gatekeeper.image" -}}
{{- $img := index .root.Values.image .component -}}
{{- $tag := required "image.tag is required (the Gatekeeper release to deploy)" .root.Values.image.tag -}}
{{- if .root.Values.image.registry -}}
{{ .root.Values.image.registry }}/{{ $img.repository }}:{{ $tag }}
{{- else -}}
{{ $img.repository }}:{{ $tag }}
{{- end -}}
{{- end }}

{{/* Where the database is: the in-cluster StatefulSet, or an external one (Cloud SQL). */}}
{{- define "gatekeeper.dbHost" -}}
{{- if .Values.database.inCluster.enabled -}}
{{ include "gatekeeper.name" (dict "root" . "component" "postgres") }}
{{- else -}}
{{ required "database.host is required when database.inCluster.enabled is false" .Values.database.host }}
{{- end -}}
{{- end }}

{{/* Pod and container security settings shared by every non-root workload. */}}
{{- define "gatekeeper.podSecurityContext" -}}
runAsNonRoot: true
seccompProfile: { type: RuntimeDefault }
{{- end }}
{{- define "gatekeeper.containerSecurityContext" -}}
allowPrivilegeEscalation: false
capabilities: { drop: [ALL] }
{{- end }}

{{/* Public URLs, without a trailing slash. */}}
{{- define "gatekeeper.appUrl" -}}
{{ required "publicUrls.app is required" .Values.publicUrls.app | trimSuffix "/" }}
{{- end }}
{{- define "gatekeeper.authUrl" -}}
{{ required "publicUrls.auth is required" .Values.publicUrls.auth | trimSuffix "/" }}
{{- end }}

{{/* Zero-downtime rollouts: add a pod before removing one. */}}
{{- define "gatekeeper.rollingUpdate" -}}
strategy:
  type: RollingUpdate
  rollingUpdate: { maxSurge: 1, maxUnavailable: 0 }
{{- end }}

{{/* Spread replicas across zones (when the cluster has several) and nodes. */}}
{{- define "gatekeeper.topologySpread" -}}
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: ScheduleAnyway
    labelSelector:
      matchLabels: {{- include "gatekeeper.selectorLabels" . | nindent 8 }}
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: ScheduleAnyway
    labelSelector:
      matchLabels: {{- include "gatekeeper.selectorLabels" . | nindent 8 }}
{{- end }}

{{/*
A terminating pod is removed from the Service at once, but a load balancer notices
a few seconds later; keep serving until it has, so no request lands on a closed port.
*/}}
{{- define "gatekeeper.gracefulStop" -}}
lifecycle:
  preStop:
    exec: { command: ["sleep", "{{ .Values.drainSeconds }}"] }
{{- end }}
