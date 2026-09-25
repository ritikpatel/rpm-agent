# RPM Platform — Agentic AI Remote Patient Monitoring (ICU & post-discharge)

AMPBA capstone for Cloud Box Technologies. A Galaxy Watch (plus 149 simulated patients) streams vitals through
edge processing and AWS IoT Core into a real-time pipeline that scores deterioration (NEWS2 + rules + ML models),
raises alerts, and hands context to Claude-based agents for triage, escalation and evidence-backed summaries.
Everything is recorded in FHIR.

- **Plan & schedule:** [docs/PLAN.md](docs/PLAN.md)
- **Architecture decisions:** [docs/adr/](docs/adr)
- **Message contracts:** [contracts/](contracts)

> All data is synthetic (MIMIC-IV-shaped). HIPAA/GDPR-aligned controls are implemented as if it were real PHI.

## Status

| Week | Scope | State |
|---|---|---|
| 1 | Contracts, simulator, IoT ingest, stream engine (NEWS2 + rules), FHIR load, local stack, AWS IaC | **done locally**; AWS apply pending credentials |
| 2 | Model serving, agents (Claude on Bedrock), RAG, alert service + escalation, clinician console | next |
| 3 | Wear OS app (Galaxy Watch + emulator), phone gateway, Greengrass ICU gateway, dashboards | |
| 4 | Security hardening, evaluation, documentation, demo | |

## Repository layout

```
contracts/              JSON Schemas (source of truth) + examples
libs/rpm_contracts      Pydantic mirror of the contracts
libs/rpm_features       NEWS2 + deterministic clinical rules (pure Python; shared with notebooks/edge)
libs/rpm_mimic          MIMIC-shaped reader + deterministic demo cohort
libs/rpm_common         settings, logging, Postgres + migrations, AWS clients
simulator/              150-patient device fleet: ICU replay, home wearables, deterioration scenarios
services/iot-bridge     LOCAL ONLY: emulates the IoT Core topic rule (MQTT -> Kinesis)
services/stream-engine  Kinesis consumer: validate, dedupe, NEWS2, rules, persist, emit clinical events
services/fhir-loader    cohort -> HAPI FHIR R4 (LOINC/ICD/SNOMED) + device registry seeding
services/api            REST + WebSocket live feed + ops live board
deploy/compose          local stack      deploy/helm/rpm   Kubernetes chart      deploy/docker   image build
infra/terraform         AWS (VPC, EKS, RDS, ElastiCache, IoT Core, Kinesis, Firehose, SQS, S3, KMS, ECR)
infra/scripts           cloud-up.sh / cloud-down.sh
docs/                   plan, ADRs
```

## Local quickstart (≈5 minutes)

Prerequisites: Docker Desktop (give it ≥ 4 GB RAM), [uv](https://docs.astral.sh/uv/), `make`. Put the dataset
in `./synthetic_mimic_150patients/` (git-ignored).

```bash
make setup && make test          # install workspace, run tests
make up                          # infra + platform services
RPM_DEMO_PATIENT=auto make seed  # FHIR + registry; reserves one CHF patient for the real Galaxy Watch
RPM_DEMO_PATIENT=auto make sim   # 149 simulated devices (25 ICU bedside, 124 home wearables)
open http://localhost:8000       # live board
```

Inject deterioration live (the presenter's controls):

```bash
make scenario P=p-90000012 S=chf_decompensation RAMP=90   # ramp over 90 s
make scenario P=p-90000002 S=sos
make scenarios                                            # list all: sepsis, copd_exacerbation, af_rvr, ...
make stats                                                # latency p50/p95, NEWS2 bands, events
```

| Endpoint | URL |
|---|---|
| Live board / API | http://localhost:8000 (`/api/patients`, `/api/events`, `/api/stats`, `ws://…/ws/live`) |
| HAPI FHIR | http://localhost:8080/fhir |
| Stream-engine metrics | http://localhost:9101/metrics |
| MQTT (devices/emulator) | `localhost:1883` topics `rpm/v1/dev/{thing}/telemetry|events|cmd` |

## AWS deployment

```bash
brew install terraform awscli
export AWS_PROFILE=<profile> AWS_REGION=us-east-1
cp infra/terraform/aws/terraform.tfvars.example my.tfvars   # set admin_cidrs, budget_email
TFVARS=$PWD/my.tfvars make cloud-up    # ~25 min first time: infra, images, secrets, helm, FHIR load
make cloud-board                       # port-forward live board to http://localhost:8000
make cloud-scenario P=p-90000012 S=sepsis RAMP=90
make cloud-down                        # destroy; stops hourly charges (≈US$10/day while up)
```

`cloud-up` writes the device certificates for `gw-demo-01` (the Galaxy Watch gateway) to `.secrets/`
(git-ignored).

## How a reading flows

1. The watch (or simulator) summarises a 30 s window and publishes `rpm.telemetry.v1` over MQTT (mTLS on AWS).
2. The IoT rule adds `thing` (taken from the topic) and `ingest_ts_ms`, then writes to Kinesis and archives to S3.
3. The stream engine validates the message and de-duplicates it, then resolves device → patient server-side. It
   merges the latest vitals and scores NEWS2, then applies the rules.
4. Events with `requires_alert` go to the alert queue immediately, and every event also goes to the agent queue.
   Vitals and events are persisted, and the live board updates over WebSocket.
5. Latency is stamped at every hop (`window_end → edge_sent → ingest → processed`).
