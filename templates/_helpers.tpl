{{- define "github-runner.name" -}}
{{- printf "%s-runner" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "github-runner.selectorLabels" -}}
app.kubernetes.io/name: github-runner
app.kubernetes.io/instance: {{ .Release.Name | quote }}
{{- end -}}

{{- define "github-runner.labels" -}}
{{ include "github-runner.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{- end -}}
{{- define "github-runner.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- end -}}
{{- end -}}
