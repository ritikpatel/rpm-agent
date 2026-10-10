# Design: agentic AI remote patient monitoring

Cloud Box Technologies · ISB AMPBA capstone. This describes the system as built. Measurements are in
[evaluation-report.md](evaluation-report.md), security in [security.md](security.md).

## 1. What the system does

Patients are monitored in two settings, and a patient is in exactly one of them at a time:

- **In the ICU:** a bedside monitor reports every vital sign every few seconds.
- **At home after discharge:** a wrist wearable (a Galaxy Watch, or a simulated one) reports heart rate, SpO2 and
  activity every 30 seconds.

Readings stream to the platform, where deterministic clinical rules decide within a second whether the care team
must be paged. Three risk models score each patient. When an alert opens, four AI agents assess the situation, act
within strict policy and write a cited handover summary. Admissions, discharges and lab results arrive from the
hospital record over FHIR, and the platform writes its results back the same way.

Three rules shape the design:

1. **Rules alert, agents advise.** Paging never waits on a language model. Agents can raise an alert's urgency;
   they cannot lower it or close it.
2. **Agents run per alert, not per reading.** Model cost and delay stay off the real-time path.
3. **Agents reach patient data only through the Care API.** Every read and action is checked and audited under the
   agent's own name; the agent service has no database access.

## 2. Architecture

![Architecture](architecture-simple.png)

Source: [architecture-simple.mmd](architecture-simple.mmd).

## 3. How one reading flows

| Step | What happens |
|---|---|
| ① | A device publishes a reading to AWS IoT Core over MQTT with TLS. EHR and lab records enter through the FHIR server. |
| ② | Both land on one SQS queue, `patient-events`, in one envelope. The IoT rule stamps the device and arrival time; FHIR changes arrive through a subscription to the Care API, which queues them. |
| ③ | The stream processor validates the event, stores it and computes the NEWS2 score. |
| ④ | It asks the risk service for scores in the background: early deterioration on new vitals and labs, readmission at discharge and daily, recovery every 15 minutes for patients at home. |
| ⑤ | If NEWS2 or a rule crosses its threshold, the processor hands a clinical event to the Care API. A validated model entering its high band does the same. |
| ⑥ | The Care API opens (or updates) the patient's alert, pushes it to the console and e-mails the on-call tier. An unacknowledged alert escalates to the next tier on a timer. |
| ⑦ | The Care API queues one agent task per alert. |
| ⑧ | Monitoring, Risk, Escalation and Summary agents run in a fixed chain on Amazon Bedrock. |
| ⑨ | The agents read through Care API tools, search the protocols, and save a cited summary plus their escalation decision, which passes a deterministic policy gate first. |
| ⑩ | Clinicians message the patient's watch; check-in answers come back over the same channel. |

A discharge is step ①–③ with a different event: the hospital finishes the FHIR Encounter, the processor moves the
patient to home monitoring, the ICU monitor stops being that patient's device, the wearable takes over, and the
readmission model runs. Nothing measured in the ICU is carried into home scoring.

## 4. Components

| Component | Responsibility | Code |
|---|---|---|
| Stream processor | Validate, de-duplicate, resolve device to patient, NEWS2, alert rules, care-setting changes, lab history | `services/stream-processor` |
| Risk service | Load model plug-ins, build the patient context, score, band, explain | `services/risk-service`, `models/plugins` |
| Care API | One alert per patient, escalation timers, notifications, sign-in and roles, audit, live WebSocket, FHIR hook and write-back, agent tools, device messages | `services/care-api` |
| Agent service | Four agents as bounded tool loops on Bedrock, policy gate, citation check, deterministic fallback | `services/agent-service` |
| Console | Worklist, patient view, alert actions, messages, demo controls, audit trail | `apps/console` |
| FHIR server | Hospital record stand-in: patients, encounters, conditions, labs, medications; rest-hook subscriptions | HAPI FHIR (off the shelf) |
| Database | Vitals, scores, alerts, audit log, guideline index (pgvector) | PostgreSQL (off the shelf) |
| Devices | Wear OS app; simulated wearables and ICU monitors | `edge/android`, `simulator` |

### The agents

| Agent | Question | Reads | Model |
|---|---|---|---|
| Monitoring | What is happening, and is the signal trustworthy? | vitals, recent events | Claude Haiku 4.5 |
| Risk | How serious is it, given history and the risk models? | risk scores, record, labs | Claude Haiku 4.5 |
| Escalation | Who must act, how fast, what reduces uncertainty? | alert state, protocols | Claude Sonnet 4.6 |
| Summary | Cited SBAR handover for the on-call team | protocols, discharge summary, record | Claude Sonnet 4.6 |

Each agent may make a few tool calls and must then submit a structured result. If a model call fails, Amazon Nova
Lite is tried; if that fails too, the run degrades to deterministic rules and a templated summary, labelled as such.
The models are configuration (`RPM_MODEL_FAST`, `RPM_MODEL_REASONING`).

## 5. Deliverables in the brief

| Deliverable | Where it lives | Coverage |
|---|---|---|
| Real-time patient monitoring | IoT Core → SQS → stream processor → console | Full |
| Agentic AI clinical monitoring engine | Agent service, four agents | Full |
| Early warning and alert system | Alert rules, Care API escalation, console and e-mail | Full; no SMS (blocked on the Free plan) |
| Predictive risk modelling | Risk service with three model plug-ins | Three models; placeholders until the team's models arrive |
| FHIR-based integration | HAPI FHIR, subscriptions in, write-back out | Full |
| RAG-powered clinical summarisation | Summary agent, pgvector, Titan embeddings, citation check | Full |
| Smart hospital connectivity | Live console, two-way messages to the watch | Messaging; no video |
| Multi-source data pipeline | The single `patient-events` queue | Full |
| Scalable microservices (Docker/Kubernetes) | Four services in containers; docker compose locally, k3s on AWS | One node, so no real high availability |
| Evaluation and validation | `tools/eval`, `make e2e` | Alert quality and latency measured; clinical impact simulated only |
| Security and compliance | Cognito, TLS, encryption at rest, audit trail | Aligned by design; synthetic data only |

## 6. Deployment

| | Local (`make up`) | AWS |
|---|---|---|
| Devices | Simulator; Wear OS emulator or watch | Simulator in the cluster and from a laptop; Galaxy Watch |
| Ingest | Mosquitto, rule emulator, ElasticMQ | IoT Core, IoT rule, SQS |
| Compute | docker compose | k3s (Kubernetes) on one EC2 instance |
| Data | PostgreSQL with pgvector, HAPI FHIR | The same containers, on the instance's encrypted disk |
| Models | Bedrock with your sign-in, or offline | Bedrock through the instance role |
| Sign-in | Dev tokens minted by the Care API (same checks) | Cognito user pool |
| E-mail | Mailpit | Amazon SES |

## 7. Decisions and their trade-offs

| Decision | Why | Cost |
|---|---|---|
| One queue for every source | One consumer, one event format, one place to reason about ordering and duplicates | Device and EHR events share a backlog |
| Rules page, agents advise | Safety and latency must not depend on a language model | Agents cannot stop a false alert; a clinician resolves it |
| Agents behind Care API tools | Audited, least-privilege access; the agent service holds no data | An extra network hop per tool call |
| Fixed chain of four agents | Easy to explain, test and bound in cost | Less flexible than a free-form planner |
| Models as plug-ins with their own feature code | The data team ships a folder; training and serving share one feature file | Feature code runs inside the risk service, so it is trusted code |
| One PostgreSQL for everything | One thing to run and back up | Services share a database, which strict microservice design avoids |
| k3s on one instance | Real Kubernetes at about US$1 a day | No tolerance of node failure |
| One image for all Python services | One build, one pull | Each container carries code it does not use |
