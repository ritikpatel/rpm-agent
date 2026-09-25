{{- define "rpm.image" -}}
{{- $root := index . 0 -}}{{- $name := index . 1 -}}
{{- if $root.Values.image.registry -}}{{ $root.Values.image.registry }}/rpm/{{ $name }}:{{ $root.Values.image.tag }}{{- else -}}rpm/{{ $name }}:{{ $root.Values.image.tag }}{{- end -}}
{{- end -}}

{{- define "rpm.labels" -}}
app.kubernetes.io/part-of: rpm
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{/* Common environment for Python services */}}
{{- define "rpm.env" -}}
- name: RPM_ENV
  value: {{ .Values.config.env | quote }}
- name: RPM_LOG_LEVEL
  value: {{ .Values.config.logLevel | quote }}
- name: RPM_AWS_REGION
  value: {{ .Values.aws.region | quote }}
- name: AWS_REGION
  value: {{ .Values.aws.region | quote }}
- name: RPM_KINESIS_STREAM
  value: {{ .Values.config.kinesisStream | quote }}
- name: RPM_SQS_CLINICAL_EVENTS_QUEUE
  value: {{ .Values.config.clinicalQueue | quote }}
- name: RPM_SQS_ALERT_INTAKE_QUEUE
  value: {{ .Values.config.alertQueue | quote }}
- name: RPM_REDIS_URL
  value: {{ .Values.config.redisUrl | quote }}
- name: RPM_FHIR_BASE_URL
  value: "http://fhir:8080/fhir"
- name: RPM_DATABASE_URL
  valueFrom: { secretKeyRef: { name: {{ .Values.secrets.db }}, key: DATABASE_URL } }
{{- end -}}
