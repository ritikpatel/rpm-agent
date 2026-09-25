# Agentic AI Remote Patient Monitoring — Build Plan

Client brief: *An Agentic AI Platform for Remote Patient Monitoring in ICU and Post-Discharge Care* (Cloud Box Technologies, AMPBA capstone).
Status: **approved** · Week 1 delivered locally (2026-09-25) · see §16 for progress

---

## 1. Locked decisions

| Topic | Decision |
|---|---|
| Wearable | **Samsung Galaxy Watch** (Samsung Health Sensor SDK) as primary, with an **alternate Wear OS emulator pathway** (Health Services synthetic data + scripted source, direct-to-cloud transport) |
| Clinical scope | **Post-discharge is the hero flow**; **ICU covered via a simulated bedside gateway** on AWS IoT Greengrass |
| Cloud | **AWS EKS**, spun up for demo windows and torn down in between (`make cloud-up` / `make cloud-down`) |
| Timeline | **< 4 weeks** → core path first, strict cut list (§13) |
| LLM | Claude on Amazon Bedrock (Sonnet-class for reasoning, Haiku-class for high-volume triage) |
| Region | `us-east-1` (widest Bedrock availability); `me-central-1` revisit only if UAE residency is demanded |
| Data | Synthetic MIMIC-IV-shaped cohort only (150 patients / 250 admissions). No real PHI, no BAA. Models are delivered by the data team and plug in via a contract (§6.1) |

## 2. Design principles

1. **Safety net first, AI second.** Deterministic rules + NEWS2 raise alerts independently of the LLM. Agents enrich, prioritise, explain and act; they may escalate but can never silently suppress a rule-fired critical alert (only a clinician can). Bedrock outage ⇒ graceful degradation to rule-based alerting.
2. **One canonical event contract.** Real watch, emulator, simulator and ICU gateway all publish `rpm.telemetry.v1`, so 1 real patient + 149 simulated patients coexist in one demo.
3. **FHIR is the clinical system of record.** High-frequency vitals live in a partitioned time-series table; derived facts are written back to FHIR (`Observation`, `RiskAssessment`, `Flag`, `Communication`, `AuditEvent`).
4. **Models are plug-ins** behind a versioned contract; NEWS2 + stub models ship so the system runs end-to-end before real models land.
5. **Local ≈ cloud.** `docker compose` stack (Mosquitto ↔ IoT Core, LocalStack ↔ Kinesis/S3/SQS) runs the same images that Helm deploys to EKS.
6. **PHI minimisation.** No patient identifiers in MQTT topics or device payloads; device→patient mapping is server-side only.

## 3. Architecture overview

```
 EDGE (home / hospital)                                   AWS
 ┌──────────────┐ BLE/Data  ┌───────────────┐  MQTT/mTLS  ┌──────────────┐  rule   ┌──────────┐
 │ Galaxy Watch │──Layer──▶│ Phone gateway │────────────▶│ AWS IoT Core │───────▶│ Kinesis  │──▶ Firehose → S3 lake
 │ (or emulator)│◀─────────│ (patient app) │◀── cmd ─────│ shadow/rules │        └────┬─────┘
 └──────┬───────┘          └───────────────┘             └──────▲───────┘             │
        └── direct MQTT (emulator / standalone Wi-Fi) ───────────┘                     ▼
 ┌──────────────┐  HL7 v2  ┌───────────────┐                    │            ┌────────────────┐   ┌───────────────┐
 │ ICU monitor  │─────────▶│ Greengrass    │────────────────────┘            │ stream-engine  │──▶│ model-serving │
 │ simulator    │  (MLLP)  │ ICU gateway   │                                 │ NEWS2, rules,  │   └───────────────┘
 └──────────────┘          └───────────────┘                                 │ windows, feats │
 ┌──────────────┐                                                            └──┬─────────┬───┘
 │ Fleet sim    │──── MQTT (149 virtual devices, same schema) ──▶ IoT Core      │ SQS     │ Redis pub/sub
 └──────────────┘                                                               ▼         ▼
                                                  ┌─────────────────────────────────┐  ┌──────────────┐
                                                  │ agent-service (LangGraph+Claude)│  │ api (REST+WS)│◀── clinician console
                                                  │ monitor · risk · escalate ·     │  └──────▲───────┘    patient app
                                                  │ summarise(RAG) · engage         │         │
                                                  └───┬───────────┬─────────────┬───┘         │
                                                      ▼           ▼             ▼             │
                                                 HAPI FHIR   pgvector RAG   alert-service ────┘
                                                                            Step Functions timers
                                                                            SES / SNS / web push / watch cmd
 Cross-cutting: Cognito · KMS · WAF · audit log (AuditEvent + CloudTrail + S3 Object Lock) · OpenTelemetry → Prometheus/Grafana
```

## 4. Edge & IoT integration (detailed)

### 4.1 Watch app (Kotlin, Wear OS, Compose for Wear)
- **Sensor sources** (one interface `VitalsSource`, chosen at runtime):
  - `SamsungSensorSource` — Samsung Health Sensor SDK: continuous HR + IBI (→ HRV), on-demand SpO₂, skin temperature (Watch5+), accelerometer. Requires Samsung Health developer mode on the watch.
  - `HealthServicesSource` — Wear OS Health Services (`MeasureClient`/`ExerciseClient`): HR, steps, activity. Used on non-Samsung watches and on the **emulator** (Android Studio → Wear Health Services synthetic data panel).
  - `ScriptedSource` — replays a deterioration script (incl. SpO₂) for emulator demos where a sensor isn't available.
- **Edge processing:** 1 s sampling → 30 s windows (15 s in *elevated*, 5 s in *critical* profile); signal-quality index using accelerometer motion to reject artefacts; local rules (sustained tachy/bradycardia, SpO₂ < 90 %, off-wrist) → haptic alert; SOS tile; fall event.
- **Transports** (interface `Transport`): `DataLayerTransport` (watch → paired phone) for the Galaxy Watch path; `DirectMqttTransport` (watch → IoT Core over Wi-Fi) for the emulator/standalone path — no pairing required.
- Runs as a foreground service (health type) with battery-aware profiles.

### 4.2 Phone gateway / patient app (Kotlin, Android)
- Receives watch data over Wearable Data Layer; **store-and-forward outbox** (Room) with backfill after offline periods; WorkManager retries.
- MQTT 3.1.1 over TLS 1.2 with **X.509 client cert** (private key in Android Keystore); priority lane for `events`.
- Syncs the **Device Shadow** (monitoring profile) and relays downlink commands to the watch.
- Patient UI: current vitals, check-in questions, care-team messages, manual entry of home BP / temperature (feeds NEWS2).
- Stretch: TFLite anomaly model against personal baseline; QR enrolment via fleet provisioning.

### 4.3 AWS IoT Core contract
| Item | Spec |
|---|---|
| Identity | 1 IoT Thing + X.509 cert per device; policy scoped to `rpm/v1/dev/${iot:Connection.Thing.ThingName}/*`. Simulator uses a fleet identity scoped to `rpm/v1/dev/sim-*` and shards across connections (IoT Core limit ~100 publishes/s per connection). |
| Uplink topics | `rpm/v1/dev/{thing}/telemetry` (QoS 1) · `rpm/v1/dev/{thing}/events` (QoS 1: `SOS`, `FALL`, `EDGE_ALERT`, `CHECKIN_REPLY`, `DEVICE_STATUS`) |
| Downlink | `rpm/v1/dev/{thing}/cmd`: `SET_PROFILE`, `SPOT_CHECK`, `CHECKIN`, `NOTIFY`, (stretch) `INCOMING_CALL` |
| Shadow (desired) | `{profile, summary_window_s, spo2_spot_interval_min, thresholds{hr_high, hr_low, spo2_low, sustain_s}}` — agents change it; device reports back |
| Rule | `SELECT *, timestamp() AS ingest_ts_ms, topic(4) AS thing FROM 'rpm/v1/dev/+/telemetry'` → Kinesis (partition key = `thing`) + Firehose → S3. Thing name comes from the **topic**, not the payload, so a device cannot spoof another. |

### 4.4 Canonical telemetry event `rpm.telemetry.v1`
```json
{
  "schema": "rpm.telemetry.v1",
  "msg_id": "01920c4e-7b1a-7c3e-9f00-2b6c1d0e4a11",
  "device_id": "gw6-a1b2c3",
  "seq": 18234,
  "source": "galaxy_watch",
  "ts": {
    "window_start": "2026-10-02T10:15:00Z",
    "window_end": "2026-10-02T10:15:30Z",
    "edge_sent": "2026-10-02T10:15:30.412Z"
  },
  "metrics": {
    "heart_rate": { "mean": 92, "min": 85, "max": 104, "n": 30, "unit": "/min", "quality": 0.93 },
    "hrv_rmssd":  { "value": 28.4, "unit": "ms", "quality": 0.81 },
    "spo2":       { "value": 93, "unit": "%", "quality": 0.88, "spot_check": true },
    "skin_temp":  { "value": 33.9, "unit": "Cel" },
    "steps":      { "value": 12 },
    "activity":   { "value": "resting" }
  },
  "edge": { "flags": ["HR_SUSTAINED_HIGH"], "profile": "normal", "buffered": false },
  "device": { "battery_pct": 64, "on_wrist": true }
}
```
`source` ∈ `galaxy_watch | wearos_hs | wearos_emulator | simulator | icu_gateway`. `msg_id` (UUIDv7) gives idempotency; `seq` gives gap detection. ICU and simulator events add `rr`, `sbp`, `dbp`, `temp`, `gcs` metrics.

### 4.5 Latency instrumentation
Timestamps: `window_end` (sensor) → `edge_sent` → `ingest_ts_ms` (IoT rule) → `processed_at` → `alert_created_at` → `delivered_at` → `acked_at`. Exported as OpenTelemetry histograms → Grafana p50/p95 per hop. This is the brief's *alert latency* metric.

### 4.6 ICU bedside gateway (AWS IoT Greengrass v2 on EC2)
Components: `hl7-listener` (MLLP server for `ORU^R01` from the ICU monitor simulator) → `bedside-rules` (local NEWS2 + alarm, works with cloud link down) → `uplink` (canonical events via Greengrass IPC; Stream Manager buffers when offline). Fallback if time runs short: same Python code as a plain container with an MQTT client.

### 4.7 Fleet simulator
Replays `chartevents` (HR 220045, SpO₂ 220277, RR 220210, NIBP 220179/220180, Temp 223762, GCS 2207xx/2239xx) for all 150 patients at accelerated time, plus scripted scenarios: sepsis, CHF decompensation, COPD exacerbation, arrhythmia, device-offline, motion artefact. Same certs/topics/schema as real devices; a scenario can be triggered live from a CLI or the console during the demo.

## 5. Cloud data plane
- **Kinesis Data Streams** (telemetry, partitioned by device) → **stream-engine** on EKS (KCL-managed leases/checkpoints): validate + dedupe (`msg_id`), map device→patient, windowed features, **NEWS2** (full for ICU/simulator; *partial NEWS2* for post-discharge using watch vitals + patient-entered BP/temp — documented limitation), threshold/trend rules, model scoring calls.
- Writes: `vitals` (PostgreSQL, daily partitions), latest state + rolling windows (Redis), FHIR `Observation` (downsampled, e.g. 5-min) to HAPI.
- Emits: live updates (Redis pub/sub → WebSocket), **clinical events** to **SQS** (with DLQ) → agent-service; rule-level critical alerts go **directly** to alert-service (bypassing the LLM).
- Raw archive: Firehose → S3 (Parquet) for retraining/evaluation.

## 6. Intelligence layer

### 6.1 Model plug-in contract
- Registry: MLflow (artifacts in S3). Each model ships `model_card.md`, `feature_spec.json` (names, types, units, windows), output schema `{score, calibrated_prob, band, top_factors[], model_version}`.
- Shared `rpm_features` Python package used by both stream-engine and the data team's notebooks (no train/serve skew).
- Models: `icu_mortality`, `readmission_30d`, `instability_6h`, `anomaly_personal`. Stubs: NEWS2-derived + simple logistic baselines until real models arrive.
- Served by **model-serving** (FastAPI, ONNX/pyfunc), writes FHIR `RiskAssessment`.

### 6.2 Agents (LangGraph, Claude on Bedrock)
| Agent | Trigger | Actions (tools) |
|---|---|---|
| Monitoring | SQS clinical event (rule breach, NEWS2 delta, risk jump, anomaly, device silent) | read trends, read FHIR context, classify real vs artefact, request `SPOT_CHECK` |
| Risk | Monitoring hand-off, discharge, daily | call models, interpret `top_factors`, write `RiskAssessment` |
| Escalation | Risk/Monitoring output | policy-bounded tier (patient nudge → nurse → physician → emergency), dedupe/hysteresis, set shadow profile, create alert |
| Summarisation (RAG) | alert created, daily, on demand | "why this alert" brief, daily post-discharge report, handover — every claim cited |
| Patient engagement | escalation / schedule | `CHECKIN` question to watch, reminders, education (patient-safe language, no diagnosis) |

Guardrails: Bedrock Guardrails (PII, denied topics), Pydantic-validated outputs, escalation policy table the LLM cannot override downward, human approval for clinical recommendations, full traces (OpenTelemetry) stored for audit. Stretch: expose the tool layer as an MCP server for a clinician copilot chat.

### 6.3 RAG
- Corpora: (a) patient record via FHIR tool calls (structured, not embedded); (b) synthetic discharge summaries generated from structured data (dataset has no notes table); (c) openly licensed guidelines (NICE NEWS2, WHO, CDC sepsis, public COPD/HF self-care material).
- pgvector in RDS, Bedrock Titan embeddings, hybrid (Postgres FTS + vector) retrieval, rerank, citation enforcement (answer rejected if a claim lacks a source).

## 7. Alerts & connectivity
- **alert-service:** lifecycle `detected → notified → acknowledged → escalated → resolved`; each alert starts a **Step Functions** execution (wait-for-ack via task token → next tier on timeout). Writes FHIR `Flag` + `Communication`.
- **Channels:** SES email, SNS SMS (sandbox, verified numbers), web push to console, watch `NOTIFY`/`CHECKIN` downlink. Emergency team = simulated webhook.
- **Clinician console (React + Vite):** patient list sorted by risk, live vitals (WebSocket), alert inbox with ack/escalate, patient timeline, AI brief with citations, device status.
- **api:** REST + WebSocket BFF, Cognito JWT auth, RBAC (clinician, nurse, admin, patient), audit middleware on every PHI read.

## 8. FHIR mapping (HAPI FHIR R4)
| Source | FHIR resource | Codes |
|---|---|---|
| patients | `Patient` | — |
| admissions / icustays / transfers | `Encounter` (+ `Location`) | — |
| diagnoses_icd | `Condition` | ICD-9/10 |
| labevents | `Observation` (laboratory) | local itemid → LOINC where mapped |
| chartevents / wearable | `Observation` (vital-signs) | HR 8867-4, SpO₂ 59408-5, RR 9279-1, Temp 8310-5, SBP 8480-6, DBP 8462-4, GCS 9269-2, Steps 55423-8, Skin temp 39106-0 |
| prescriptions / emar | `MedicationRequest` / `MedicationAdministration` | NDC |
| procedures_icd | `Procedure` | ICD |
| omr | `Observation` (weight, BMI, BP) | Weight 29463-7 |
| devices | `Device` (patient link) | — |
| model outputs | `RiskAssessment` | — |
| alerts | `Flag`, `Communication`, `Task` | — |
| access | `AuditEvent` | — |

## 9. AWS deployment
- **Terraform modules:** `network` (VPC, private subnets, 1 NAT, VPC endpoints), `eks` (managed node group, spot), `data` (RDS PostgreSQL 16 + pgvector, ElastiCache Redis), `iot` (things, policies, rules, Kinesis, Firehose, S3), `messaging` (SQS, Step Functions, SES, SNS), `security` (KMS, Cognito, WAF, Secrets Manager, CloudTrail), `greengrass` (EC2 core device), `ecr`.
- **Helm:** one chart per service + umbrella chart; IRSA/Pod Identity for AWS access; ALB ingress; kube-prometheus-stack + Grafana dashboards.
- **CI/CD:** GitHub Actions → test → build → ECR → `helm upgrade`.
- **Cost:** roughly US$300–450/month if left running 24/7 (EKS control plane ≈ $73 of that); run only in demo windows. Budget alarm at a threshold you choose.

## 10. Security & compliance (HIPAA/GDPR-aligned, synthetic data)
TLS 1.2+ everywhere and mTLS for devices; KMS CMKs for RDS/S3/Kinesis/EBS; Cognito + RBAC; private subnets, security groups, WAF; Secrets Manager; least-privilege IAM per pod; PHI-free topics/payloads; application audit log of every PHI access (FHIR `AuditEvent`) + CloudTrail, shipped to S3 with Object Lock; FHIR `Consent`; GDPR export/delete endpoints (stretch). Deliverables: HIPAA safeguard ↔ control matrix, GDPR article mapping, STRIDE threat model, data-flow diagram.

## 11. Evaluation & validation
| Dimension | Metrics / method |
|---|---|
| Prediction (harness for data-team models) | AUROC, AUPRC, calibration (Brier, reliability), sensitivity @ fixed specificity, lead time, subgroup fairness |
| Alerting | End-to-end latency p50/p95 per hop, alerts per patient-day (alarm burden), alert PPV vs scenario ground truth, time-to-ack, escalation rate |
| Agents / LLM | Summary faithfulness & citation accuracy (LLM-judge + spot review), escalation agreement on a golden scenario set, guardrail red-team cases, tokens & cost per patient-day |
| System | Load test 1k → 10k simulated patients; chaos: phone offline → backfill with zero loss, pod kill, Bedrock unavailable → rules still alert |
| Clinical impact (retrospective) | Replay cohort: hours of warning before readmission / death events; estimated readmissions flagged |

## 12. Repository layout
```
contracts/            event schemas (JSON Schema), FHIR mappings, model contract
edge/wearos-app/      Kotlin watch app (Samsung + Health Services + scripted sources)
edge/phone-gateway/   Kotlin phone gateway / patient app
edge/icu-gateway/     Greengrass components + ICU monitor simulator (HL7)
simulator/            fleet simulator (MIMIC replay + scenarios)
services/stream-engine  services/model-serving  services/agent-service
services/alert-service  services/api            services/fhir-loader
libs/rpm_features/    shared feature + NEWS2 code
apps/clinician-console/ React
infra/terraform/      deploy/helm/      deploy/compose/
eval/                 evaluation harness, scenario sets, load tests
docs/                 PLAN, HLD, ADRs, compliance, runbook, demo script
```

## 13. Four-week schedule
| Week | Goal | Exit criteria |
|---|---|---|
| **1 — Data plane** | Contracts, monorepo, local compose stack, simulator, stream-engine (NEWS2 + rules), MIMIC → FHIR loader. Terraform baseline + **thin slice on AWS** (simulator → IoT Core → Kinesis → stream-engine on EKS). | 150 simulated patients streaming locally and on AWS; NEWS2 visible per patient; FHIR populated |
| **2 — Intelligence & alerts** | model-serving + stubs, agent graph (monitor, risk, escalate, summarise), RAG index, alert-service + Step Functions, email/SMS/web push, console v1 | Scripted deterioration → cited AI brief + notification + ack/escalation, end to end on AWS |
| **3 — Edge** | Wear OS app (Samsung + emulator paths), phone gateway (outbox, mTLS, shadow, downlink), device provisioning, patient check-in, Greengrass ICU gateway, latency dashboards | Real Galaxy Watch and emulator both stream to AWS; agent changes watch profile; offline → backfill works |
| **4 — Harden & prove** | Security controls, evaluation runs (latency, burden, LLM evals, load, chaos), docs (HLD, ADRs, compliance matrix, threat model), demo script + 2 dry runs | Evaluation report + demo recording; `cloud-up` from scratch < 30 min |

**Cut list if we slip (in order):** video teleconsult → ASR voice check-ins → MCP copilot chat → TFLite on-device model (rules only at edge) → Greengrass packaging (ICU gateway as plain container) → patient-engagement agent depth → CI/CD polish.

## 14. Prerequisites (owner: you)
1. **AWS:** account with admin-level IAM user/role for Terraform; AWS CLI + profile configured; **Bedrock model access** enabled for Claude (Sonnet- and Haiku-class) and Titan embeddings in `us-east-1`; SES verified sender + recipient emails; SNS SMS sandbox verified phone number(s); a budget alarm.
2. **Local tools:** install `terraform` and `awscli` (e.g. `brew install terraform awscli`); start Docker Desktop.
3. **Android:** Galaxy Watch model (4/5/6/7/Ultra?) and an Android phone; enable Samsung Health developer mode on the watch; download the **Samsung Health Sensor SDK** (Samsung developer account + licence acceptance — must be done by you) and place the `.aar` in `edge/wearos-app/libs/`.
4. **Repo:** OK to `git init` here with the dataset git-ignored; optional GitHub remote for CI.
5. **Demo date** so the schedule can be pinned to calendar dates.

## 15. Assumptions & open items
- Synthetic discharge summaries will be generated for RAG unless the data team supplies notes.
- Guideline corpus limited to openly licensed sources.
- Post-discharge NEWS2 is partial (watch lacks RR/BP/core temp); supplemented by patient-entered readings and clearly labelled.
- Data-team models will be delivered as MLflow/ONNX artefacts matching §6.1.

## 16. Progress log

### Week 1 — delivered (2026-09-25)
- [x] Contracts: `rpm.telemetry.v1`, `rpm.device_event.v1`, `rpm.command.v1`, `rpm.shadow.v1`, `rpm.clinical_event.v1`
      (JSON Schema + Pydantic, drift-tested)
- [x] `rpm_features`: NEWS2 (RCP 2017, Scale 2, partial scoring, consciousness vs baseline GCS) + rules
      (sustained HR with hysteresis, SpO₂ low/critical, NEWS2 bands/trend, alarm-fatigue suppression)
- [x] Simulator: 149 devices (25 ICU replay of MIMIC chartevents, 124 home wearables), 10 scenarios incl.
      store-and-forward backfill, motion artefact, SOS/fall; responds to SET_PROFILE / SPOT_CHECK / CHECKIN
- [x] Stream engine: Kinesis consumer with Postgres checkpoints, dedupe, spoof rejection, late/buffered handling,
      gap detection, silent-device watchdog, per-hop latency metrics
- [x] FHIR loader: 150 patients / 22,372 resources (LOINC, ICD-9/10, SNOMED) in ~21 s; device registry;
      `auto` demo patient = p-90000006 (CHF) for the Galaxy Watch
- [x] API + live board (REST, WebSocket, audit log on PHI reads)
- [x] Local stack (compose) — measured end-to-end p50 ≈ 0.26 s, p95 ≈ 0.55 s at 149 devices
- [x] Terraform (validated) + Helm chart (lint + kubeconform) + `make cloud-up / cloud-down`
- [ ] `make cloud-up` against the AWS account (waiting on credentials + Bedrock access)
- 128 unit/contract tests, ruff clean

### Changes vs. the original plan
- Local emulators are Mosquitto + rule emulator, kinesis-mock and ElasticMQ (not LocalStack) — ADR 0003.
- Graviton (arm64) nodes — ADR 0006.
- Telemetry metric names: `resp_rate`, `sbp`, `dbp`, `temp`, `gcs` (see `contracts/`).
