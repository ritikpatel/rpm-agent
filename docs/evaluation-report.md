# Evaluation report

Measured on 10 October 2026 on the local stack (Docker on a MacBook, 8 GB) with synthetic data. The raw outputs are
in [eval/](eval). Every number here can be regenerated with the commands in the [runbook](runbook.md).

## Summary

| Question | Result | Source |
|---|---|---|
| Does the whole path work? | 25 of 25 end-to-end checks pass, with the Claude agents on Amazon Bedrock | `make e2e` |
| How fast is a page? | Median 0.34 s from sensor reading to console page, 0.62 s to e-mail; worst case seen 1.7 s. Target: under 5 s | `eval/latency.json` |
| Are deteriorations caught? | 34 of 36 injected deteriorations detected (94%) | `eval/replay.json` |
| Do harmless events alert? | 0 of 10 motion-artefact and connectivity-loss cases alerted | `eval/replay.json` |
| How many alerts does a team get? | 0.66 per patient per day at home; 4.6 per patient per day in ICU (see below) | `eval/replay.json` |
| Are the agents' citations real? | 23 of 23 citations checked resolve to a retrieved passage | `eval/agents.json` |
| Do the guardrails hold? | 13 of 13 red-team cases held | `eval/guardrails.json` |
| How accurate are the risk models? | Not established: the three models are placeholders | `eval/models.json` |

## 1. Prediction accuracy

The three models in `models/plugins/` are placeholders so the platform runs end to end. They are not validated and
their numbers say nothing about the models the team is training.

| Model | Owner of the real model | Placeholder | Cross-validated AUROC |
|---|---|---|---|
| Early deterioration | Harnoor | Logistic model with hand-set weights | none (no outcome labels in the extract) |
| Readmission risk | Tiya | Logistic regression on 90 discharges, 28 readmitted | 0.39 |
| Recovery monitoring | Navya | Logistic model with hand-set weights | none (no outcome labels in the extract) |

An AUROC of 0.39 is worse than chance: the synthetic extract carries no learnable readmission signal with these
features. This is why every placeholder is marked `validated: false`, is labelled as weak evidence in the console
and to the agents, and cannot page anyone.

When a real model arrives, its `metadata.json` carries its own metrics, `rpm-eval models` reports them, and
`validated: true` lets it raise alerts. The platform does not re-measure model accuracy; the model owner does.

## 2. Alert latency

Each hop stamps a time, so latency is read from stored data rather than estimated.

| Path | Median | 95th percentile | Note |
|---|---|---|---|
| Device to platform (uplink) | 0.004 s | 0.05 s | Local broker; over the internet to AWS IoT Core this will be larger |
| Queue to scored (pipeline) | 0.018 s | under 0.05 s in steady state | Rises to seconds only while a restarted processor drains a backlog |
| Sensor reading to console page | 0.34 s | 1.6 s | Includes a 0.25 s settle so one page carries the final severity |
| Sensor reading to e-mail | 0.62 s | 1.6 s | Local mail server; Amazon SES adds its own delivery time |
| Alert to agents' summary | 50 s | 60 s | Four agents on Bedrock; does not delay the page |

The safety-relevant figure is sensor to page, and it never depends on the agents. The 20-minute measurement window
in `eval/latency.json` includes several service restarts made during testing, which is why its pipeline p95 shows
seconds; the end-to-end check on an undisturbed stack measured p95 of 0.04 s from sensor to scored.

## 3. Alert quality

Method: the real simulator and the real stream processor are run in memory on a virtual clock for a simulated 24
hours with all 150 patients (125 at home, 25 in ICU). 36 deteriorations and 10 harmless disturbances are injected at
known times, so every alert can be judged against ground truth. A simulated clinician resolves each alert after 30
minutes.

| Setting | Scenario | Cases | Detected | Median time to detect |
|---|---|---|---|---|
| Home | Sepsis | 4 | 4 | 6.4 min |
| Home | Heart failure decompensation | 4 | 4 | 7.6 min |
| Home | COPD exacerbation | 4 | 4 | 6.7 min |
| Home | Hypoxia | 4 | 4 | 4.3 min |
| Home | Atrial fibrillation, fast | 4 | 3 | 1.9 min |
| Home | Bradycardia | 4 | 3 | 2.7 min |
| Home | Motion artefact (must not alert) | 5 | 0 alerts | |
| Home | Connectivity loss (must not alert) | 5 | 0 alerts | |
| ICU | Six scenario types, 2 each | 12 | 12 | 1.0 to 4.2 min |

Time to detect is measured from the start of a gradual onset, so it includes the time the illness takes to develop.

Alert burden:

| Setting | Alerts per patient per day | Share of alerts that fall inside an injected deterioration |
|---|---|---|
| Home | 0.66 | 27% |
| ICU | 4.6 | 10% |

Reading these:

- **Home** is under one alert per patient per day, which was the target. Most alerts outside injected scenarios are
  NEWS2-medium and low-SpO2 findings from the simulated patients' own baselines.
- **ICU is high.** The ICU patients replay real charted stays of very sick patients (22 of the 25 died), whose
  vital signs meet NEWS2's emergency threshold for long stretches. NEWS2 was designed for general wards; an ICU
  would set unit-specific thresholds or rely on the bedside monitor's own alarms and use this platform for remote
  oversight. The thresholds are configuration, and this is the first thing to tune with clinical input.
- **Two misses**, both at home and both heart-rhythm events of short duration on a wearable that reports every 30
  seconds.

## 4. The agents

From the runs recorded during testing (`eval/agents.json`):

| Measure | Result |
|---|---|
| Runs audited | 10 (7 with Claude on Bedrock, 3 offline) |
| Citations checked against the guideline index | 23, all valid |
| Summaries whose advice contradicts the alert severity | 0 |
| Policy violations (patient-facing action in ICU, escalation at low concern) | 0 |
| Time per alert run | median 36 to 50 s |
| Tokens per alert run | about 32,000 in, 4,400 out |

At list prices for Claude Haiku 4.5 and Sonnet 4.6 that is roughly US$0.12 per alert run (my estimate from the
measured token counts; check current Bedrock pricing). The Care API allows at most one agent run per patient every
10 minutes unless the alert's severity rises.

Guardrail red-team (`eval/guardrails.json`): 13 crafted cases, all held. They cover an agent trying to lower
severity, a prompt-injection asking for a downgrade, patient-facing actions for ICU patients, escalation without
grounds, escalation on a suspected artefact, repeated nagging of a patient, invented actions ("call ambulance",
"prescribe"), an invented citation, and an uncited recommendation. One control case confirms a legitimate critical
escalation still goes through.

## 5. Clinical outcome impact

Not measured, and it cannot be with synthetic data. What the replay does show is lead time: gradual deteriorations
are flagged four to eight minutes after onset at home and one to four minutes in ICU, with a cited summary about a
minute later. Whether earlier flags change outcomes needs a prospective study with real patients and clinicians.

## 6. What was verified by hand

- The clinician console in a browser: worklist, alert with the agents' summary and citations, demo controls.
- The watch app on the Wear OS emulator against the local stack: readings arrive, the simulator stops for that
  patient, a clinician's message and a check-in question reach the watch, the answer and an SOS come back.
- Not yet verified: a physical Galaxy Watch, and the AWS deployment.
