{{/*
Chart name.
*/}}
{{- define "servicenow-mcp.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name (release-name if it already contains the chart name).
*/}}
{{- define "servicenow-mcp.fullname" -}}
{{- if contains .Chart.Name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "servicenow-mcp.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "servicenow-mcp.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "servicenow-mcp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "servicenow-mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Name of the Secret that holds the ServiceNow credentials.
*/}}
{{- define "servicenow-mcp.secretName" -}}
{{- if .Values.servicenow.existingSecret }}
{{- .Values.servicenow.existingSecret }}
{{- else }}
{{- include "servicenow-mcp.fullname" . }}
{{- end }}
{{- end }}
