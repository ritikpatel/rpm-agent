# Agentic AI platform for remote patient monitoring (ICU and post-discharge)

AMPBA capstone for Cloud Box Technologies. Patients are monitored in two settings: in the ICU through bedside
monitors, and at home after discharge through a wrist wearable. Readings stream in real time, deterministic rules
page the care team within a second, three risk models score each patient, and four AI agents add a cited clinical
summary and an escalation recommendation. Everything is exchanged with the hospital record over FHIR.

> All data is synthetic (MIMIC-IV-shaped). The controls are designed as if it were real patient data.

![Architecture](docs/architecture-simple.png)

The diagram source is [docs/architecture-simple.mmd](docs/architecture-simple.mmd). Follow the numbers ① to ⑩ to
trace one reading from a device to a clinician.

## Where to read next

| You want to | Read |
|---|---|
| Understand the design and how it covers the brief | [docs/HLD.md](docs/HLD.md) |
| Run it on your laptop | Quick start below, then [docs/e2e-test.md](docs/e2e-test.md) |
| Hand over a model (Harnoor, Tiya, Navya) | [models/CONTRACT.md](models/CONTRACT.md) |
| Connect a real Galaxy Watch | [docs/watch-setup.md](docs/watch-setup.md) |
| Operate or deploy it | [docs/runbook.md](docs/runbook.md) |
| Present it | [docs/demo-script.md](docs/demo-script.md) |
| See the measurements | [docs/evaluation-report.md](docs/evaluation-report.md) |
| Review security and compliance | [docs/security.md](docs/security.md) |

## Quick start (local)

You need Docker Desktop (4 GB is enough), [uv](https://docs.astral.sh/uv/), and the dataset folder
`synthetic_mimic_150patients/` in the repo root (it is not in git).

```bash
make setup          # Python workspace
make models         # fit the three placeholder models
make up             # start the stack (add LLM=bedrock to use the Claude agents; see below)
make seed           # 24 patients: registry, FHIR record, guideline index
make sim            # simulated ICU monitors and wearables
make e2e            # automated end-to-end check (about 4 minutes)
```

Then open the console at <http://localhost:3000> and sign in as **Demo lead**. Alert e-mails appear at
<http://localhost:8025>, the FHIR server at <http://localhost:8080>.

By default the agents run offline (deterministic rules and a templated summary), which needs no credentials. To use
the real agents on Amazon Bedrock, sign in with `aws login --profile rpm-capstone` and start with
`make up LLM=bedrock`.

Useful while it runs:

```bash
make scenario P=p-90000132 S=sepsis RAMP=60     # make a patient deteriorate
make send P=p-90000019 ARGS="--hr 134 --spo2 86"  # send one reading right now
make cohort                                     # who is monitored
make stats                                      # latency and counts
make logs                                       # follow the services
make down                                       # stop (make reset also deletes the data)
```

## Repository layout

```
services/stream-processor   consumes the patient-events queue: validate, store, NEWS2, alert rules, care-setting changes
services/risk-service       serves the three model plug-ins: builds features, scores, explains
services/care-api           alerts and escalation, notifications, sign-in and roles, audit, FHIR hook and write-back,
                            the tools the agents use
services/agent-service      four agents on Amazon Bedrock (LangGraph); holds no patient data
apps/console                clinician console (React)
libs/rpm_core               shared code: message contracts, NEWS2, alert rules, database, guideline index
simulator                   simulated ICU monitors and wearables; also the one-shot "send" command
edge/android                Wear OS watch app (and an optional phone companion)
models/plugins              the three models, each a self-contained plug-in; models/CONTRACT.md is the hand-over spec
tools/seed                  loads the cohort into FHIR, the registry and the guideline index
tools/eval                  evaluation: alert quality by replay, latency, agent audit
tools/iot-rule-emulator     local stand-in for the AWS IoT topic rule
contracts                   JSON Schemas for every device message
knowledge/protocols         the clinical protocols the summaries cite (demo content)
deploy/compose              the local stack
```

One image (`deploy/docker/python.Dockerfile`) holds every Python service; each container runs its own entry point.

## The team's models

| Model | Owner | Status |
|---|---|---|
| Early deterioration | Harnoor | placeholder in place; drop the real plug-in into `models/plugins/` |
| Readmission risk | Tiya | placeholder in place |
| Recovery monitoring | Navya | placeholder in place |

Placeholders are labelled "not validated" everywhere they appear, and an unvalidated model can never page anyone.
