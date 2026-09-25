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
- name: RPM_MODEL_SERVING_URL
  value: "http://model-serving:8001"
- name: RPM_DEVICE_COMMAND_MODE
  value: "iot"
- name: RPM_IOT_ENDPOINT
  value: {{ .Values.config.iotEndpoint | quote }}
- name: RPM_EMBEDDINGS
  value: {{ .Values.config.embeddings | quote }}
- name: RPM_RUN_MIGRATIONS
  value: "false"
{{- end -}}

{{/* Standard single-container worker Deployment */}}
{{- define "rpm.deployment" -}}
{{- $root := index . 0 -}}{{- $name := index . 1 -}}{{- $image := index . 2 -}}{{- $cmd := index . 3 -}}
{{- $vals := index . 4 -}}{{- $extraEnv := index . 5 -}}{{- $port := index . 6 -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ $name }}
  labels: {{- include "rpm.labels" $root | nindent 4 }}
spec:
  replicas: 1
  selector: { matchLabels: { app: {{ $name }} } }
  template:
    metadata:
      labels: { app: {{ $name }} }
      annotations: { prometheus.io/scrape: "true", prometheus.io/port: "9100" }
    spec:
      serviceAccountName: {{ $name }}
      securityContext: {{- toYaml $root.Values.podSecurityContext | nindent 8 }}
      containers:
        - name: {{ $name }}
          image: {{ include "rpm.image" (list $root $image) }}
          imagePullPolicy: {{ $root.Values.image.pullPolicy }}
          command: {{ toJson $cmd }}
          env:
            {{- include "rpm.env" $root | nindent 12 }}
            {{- with $extraEnv }}{{ toYaml . | nindent 12 }}{{- end }}
          {{- if $port }}
          ports: [{ name: http, containerPort: {{ $port }} }]
          readinessProbe: { httpGet: { path: /healthz, port: http }, periodSeconds: 10 }
          {{- end }}
          securityContext: {{- toYaml $root.Values.containerSecurityContext | nindent 12 }}
          volumeMounts:
            - { name: tmp, mountPath: /tmp }
            {{- if eq $name "alert-service" }}
            - { name: care-team, mountPath: /config, readOnly: true }
            {{- end }}
          resources: {{- toYaml $vals.resources | nindent 12 }}
      volumes:
        - { name: tmp, emptyDir: {} }
        {{- if eq $name "alert-service" }}
        - { name: care-team, configMap: { name: care-team } }
        {{- end }}
{{- end -}}
