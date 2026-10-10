# RPM Platform — Agentic AI Remote Patient Monitoring (ICU & post-discharge)

AMPBA capstone for Cloud Box Technologies. A Galaxy Watch (plus 149 simulated patients) streams vitals through
edge processing and AWS IoT Core into a real-time pipeline that scores deterioration (NEWS2 + rules + ML models),
raises alerts, and hands context to Claude-based agents for triage, escalation and evidence-backed summaries.
Everything is recorded in FHIR.

- **Plan & schedule:** [docs/PLAN.md](docs/PLAN.md)
- **Design:** [docs/HLD.md](docs/HLD.md) · **decisions:** [docs/adr/](docs/adr)
- **Evaluation:** [docs/evaluation-report.md](docs/evaluation-report.md) · **security:** [docs/security/](docs/security)
- **Operations:** [docs/runbook.md](docs/runbook.md) · **demo:** [docs/demo-script.md](docs/demo-script.md)
- **Message contracts:** [contracts/](contracts)

> All data is synthetic (MIMIC-IV-shaped). HIPAA/GDPR-aligned controls are implemented as if it were real PHI.

## Status

| Week | Scope | State |
|---|---|---|
| 1 | Contracts, simulator, IoT ingest, stream engine (NEWS2 + rules), FHIR load, local stack, AWS IaC | **done locally**; AWS apply pending credentials |
| 2 | Model serving, agents (LangGraph + Claude), RAG, alert lifecycle + escalation + notifications, clinician console | **done locally** (agents verified in offline mode; live Claude run needs a key or Bedrock access) |
| 3 | Wear OS app (Galaxy Watch + emulator), phone gateway, Greengrass ICU gateway, dashboards | **done locally**: verified on the Wear OS emulator with mTLS; real watch needs the Samsung SDK `.aar` |
| 4 | Security hardening, evaluation, documentation, demo | **done locally**: auth + RBAC, WORM audit, network policies; replay-tuned alerting (0.66 alerts/patient-day at home, 35/36 detected); chaos 4/4; docs. AWS run and live Claude pending access |

## Repository layout

```
contracts/              JSON Schemas (source of truth) + examples
libs/rpm_contracts      Pydantic mirror of the contracts
libs/rpm_features       NEWS2 + deterministic clinical rules (pure Python; shared with notebooks/edge)
libs/rpm_mimic          MIMIC-shaped reader + deterministic demo cohort
libs/rpm_common         settings, logging, Postgres + migrations, AWS clients, device commands
libs/rpm_alerts         alert lifecycle, escalation policy, notification channels (outbox)
libs/rpm_rag            protocol corpus, discharge summaries, embeddings, hybrid retrieval
simulator/              150-patient device fleet: ICU replay (sent as HL7 to the ICU gateway), home wearables, scenarios
edge/android            Wear OS watch app + phone gateway + shared Kotlin edge core; provision.sh (mTLS identity)
edge/icu-gateway        ICU bedside gateway: HL7 v2 (MLLP) -> bedside NEWS2/alarms -> outbox -> AWS IoT (Greengrass)
services/iot-bridge     LOCAL ONLY: emulates the IoT Core topic rule (MQTT -> SQS ingest + archive queues)
services/stream-engine  SQS ingest consumer: validate, dedupe, NEWS2, rules, persist, emit clinical events;
                        second entrypoint `rpm-telemetry-archiver`: raw telemetry -> gzip NDJSON in the S3 lake
services/fhir-loader    cohort -> HAPI FHIR R4 (LOINC/ICD/SNOMED) + device registry seeding
services/api            REST + WebSocket live feed, clinical workflow endpoints, ops live board
services/model-serving  ONNX risk models + occlusion explanations + FHIR RiskAssessment
services/alert-service  SQS intake, one alert per patient, timed escalation, email/SMS/watch/webhook
services/agent-service  LangGraph agents (Claude via Bedrock/API, offline fallback), policy gate, cited SBAR briefs
apps/clinician-console  React console: alert inbox, patient census, vitals, briefs with evidence, actions
models/                 baseline training + model package contract     knowledge/  demo clinical protocols
deploy/compose          local stack (+ observability: Prometheus, Grafana dashboards as code)
deploy/helm/rpm         Kubernetes chart      deploy/docker   image build
infra/terraform         AWS (VPC, EKS, RDS, ElastiCache, IoT Core, SQS, S3, KMS, ECR, Cognito; optional Greengrass core)
infra/scripts           cloud-up.sh / cloud-down.sh
eval/                   evaluation harness: replay (alert quality), chaos, load, guardrails, agent audit
docs/                   plan, HLD, ADRs, evaluation report, security (threat model, compliance), runbook, demo
```

## Local quickstart (≈5 minutes)

Prerequisites: Docker Desktop (give it ≥ 4 GB RAM), [uv](https://docs.astral.sh/uv/), `make`. Put the dataset
in `./synthetic_mimic_150patients/` (git-ignored).

```bash
make setup && make test          # install workspace, run tests
make models                      # train/export baseline risk models (placeholders for the data team's)
make up                          # infra + platform services + console
RPM_DEMO_PATIENT=auto make seed  # FHIR + registry + RAG index; reserves one CHF patient for the Galaxy Watch
RPM_DEMO_PATIENT=auto make sim   # 149 simulated devices (25 ICU beds via the HL7 gateway, 124 home wearables)
open http://localhost:3000       # clinician console: dev sign-in (pick a role); email sink http://localhost:8025
make obs                         # optional: latency dashboards at http://localhost:3001
```

Agents run in **offline mode** (deterministic rules + templates) by default. To use Claude:
`RPM_LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=... make up`, or `RPM_LLM_PROVIDER=bedrock` with AWS credentials.

Inject deterioration live (the presenter's controls):

```bash
make scenario P=p-90000012 S=chf_decompensation RAMP=90   # ramp over 90 s
make scenario P=p-90000002 S=sos
make scenarios                                            # list all: sepsis, copd_exacerbation, af_rvr, ...
make stats                                                # latency p50/p95, NEWS2 bands, events
```

| Endpoint | URL |
|---|---|
| Clinician console | http://localhost:3000 (alert inbox, patient view, presenter controls) |
| Escalation emails (Mailpit) | http://localhost:8025 |
| Live board / API | http://localhost:8000 (`/docs` for the OpenAPI spec, `ws://…/ws/live`) |
| Model serving | http://localhost:8001/v1/models |
| HAPI FHIR | http://localhost:8080/fhir |
| Latency dashboards (`make obs`) | http://localhost:3001 (Grafana) · http://localhost:9090 (Prometheus) |
| ICU gateway | `localhost:2575` HL7 MLLP · http://localhost:8081/status (nurse-station view) |
| MQTT (devices/emulator) | `localhost:1883` plain · `localhost:8883` mTLS (after `edge/android/provision.sh local`) |

## AWS deployment

```bash
brew install terraform awscli
export AWS_PROFILE=rpm-capstone        # Region comes from the profile (ap-southeast-2); override with AWS_REGION
cp infra/terraform/aws/terraform.tfvars.example my.tfvars   # set admin_cidrs, budget_email
TFVARS=$PWD/my.tfvars make cloud-up    # ~25 min first time: infra, images, secrets, helm, FHIR load
make cloud-board                       # port-forward the clinician console to http://localhost:3000
make cloud-scenario P=p-90000012 S=sepsis RAMP=90
make cloud-down                        # destroy; stops hourly charges (≈US$10/day while up)
```

`cloud-up` writes the device certificates for `gw-demo-01` (the watch) and `icu-gw-01` (the ICU gateway) to
`.secrets/` (git-ignored). Then `edge/android/provision.sh aws` puts the watch's identity on the device (see
[edge/android/README.md](edge/android/README.md)).

Options:

- `OBSERVABILITY=1` adds kube-prometheus-stack with the latency dashboard.
- `enable_greengrass_core = true` in the tfvars runs the ICU gateway as a Greengrass v2 component on a t4g.small
  instead of a pod.

The deployment uses only services on the Free plan of the new AWS experience and lives in one Region
(`ap-southeast-2` for this project). What that plan rules out, and what the platform does instead, is in
[docs/runbook.md](docs/runbook.md#aws-plan-limits-new-aws-experience-free-plan) and
[ADR 0014](docs/adr/0014-ingest-on-sqs.md).

## How a reading flows

1. The watch (or simulator) summarises a 30 s window and publishes `rpm.telemetry.v1` over MQTT (mTLS on AWS).
   ICU monitors send HL7 v2 to the bedside gateway, which windows the readings, raises bedside alarms, and publishes
   for its beds as its own thing.
2. The IoT rule adds `thing` (taken from the topic) and `ingest_ts_ms`, then writes the message to two SQS
   queues: one feeds the stream engine, the other feeds the archiver, which batches raw telemetry into S3.
3. The stream engine validates the message and de-duplicates it, then resolves device → patient server-side. It
   merges the latest vitals and scores NEWS2, then applies the rules.
4. Events with `requires_alert` go to the alert queue immediately, and every event also goes to the agent queue.
   Vitals and events are persisted, and the live board updates over WebSocket.
5. The **alert service** opens or updates the patient's single active alert, pages the tier for its severity
   (email, SMS, console, and a reassurance message on the patient's watch), and escalates if nobody acknowledges.
6. The **agents** score risk, assess the event and act within policy: an on-demand SpO₂ check, a symptom
   check-in on the watch, faster sampling, or escalation. They then attach a cited SBAR brief to the alert.
7. Latency is stamped at every hop and shown in Grafana. Locally, at p95: sensor → scored ≈ 1.0 s (home) and
   1.4 s (ICU), sensor → page ≈ 1.9 s.
