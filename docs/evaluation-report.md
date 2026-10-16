# Evaluation report

Measured on 10 October 2026 on the local stack (Docker on a MacBook, 8 GB) with the patients in the team's training
data; the alert-quality replay was re-run on 16 October with the current rules and models, with the same results. The raw outputs are in [eval/](eval). Every number here can be regenerated with the commands in the
[runbook](runbook.md).

## Summary

| Question | Result | Source |
|---|---|---|
| Does the whole path work? | 25 of 25 end-to-end checks pass, with the Claude agents on Amazon Bedrock and real alert e-mail | `make e2e` |
| How fast is a page? | Median 0.3 s from sensor reading to console page. Target: under 5 s | `eval/latency.json` |
| Are the data's own deteriorations caught? | 55 of the 56 patients whose data deteriorates were alerted (98%) | `eval/replay.json` |
| Do stable patients alert? | 0 of the 116 patients whose data is stable throughout | `eval/replay.json` |
| Are injected deteriorations caught? | 29 of 30; the miss is one fast-heart-rhythm case in ICU | `eval/replay.json` |
| Do harmless events alert? | 0 of 10 motion-artefact and connectivity-loss cases | `eval/replay.json` |
| Should the risk models confirm alerts? | Not on this data: holding a partial NEWS2 until a model agrees missed 2 of the 55 deteriorating patients and saved only those 2 alerts; also gating single vital-sign rules missed every slow-heart-rate case. Switched off (`RPM_MODEL_CONFIRMED_ALERTS`) | `eval/replay-model-confirmation-*.json` |
| How many alerts does a team get? | 0.61 per patient per day at home, 0.75 in ICU | `eval/replay.json` |
| Are the agents' citations real? | 96 of 96 citations checked resolve to a retrieved passage | `eval/agents.json` |
| Do the guardrails hold? | 13 of 13 red-team cases held | `eval/guardrails.json` |
| How accurate are the risk models? | Recovery monitoring: held-out AUROC 0.91. Readmission: 0.97 on 16 admissions (3 events). Early deterioration: chance level on the shared data | model cards, `services/risk-service/tests/test_team_models.py` |

## 1. Prediction accuracy

The three models are the team's, handed over on 2026-10-14 and served unchanged from their LightGBM files. The first
column is each owner's held-out result; the next two were measured on the platform from the shared training data.

| Model | Owner's held-out result | Platform rebuilds the inputs it was trained on | Scored through the platform's feature path |
|---|---|---|---|
| Recovery monitoring (Navya) | AUROC 0.91, AUPRC 0.84 (330 snapshots, 86 events); 5-fold CV AUROC 0.87 | Yes: matches her training table to 6 decimal places | AUROC 0.99 (training data, optimistic) |
| Readmission risk (Tiya) | AUROC 0.97 on 16 admissions, 3 readmissions | Yes: all 63 labelled admissions score identically | AUROC above 0.9 (training data, optimistic) |
| Early deterioration, 6 h (Harnoor) | AUROC 0.92, AUPRC 0.63, on a 300-patient file not in the hand-over | Partly: the platform has no activity, steps or patient-reported scores, so those are carried forward from the record | AUROC 0.51 on the shared data with its own columns; 0.75 on the MIMIC snapshots through the platform's features |

Recovery monitoring has held-out evidence on the data the platform runs on, so it is marked validated: a high score
can raise an alert. Readmission risk has too few events (ten readmissions in all) and early deterioration does not
hold up on the shared data, so both are marked `validated: false`: labelled as weak evidence in the console and to
the agents, and they cannot page anyone. What would make early deterioration usable is retraining on the shared file
with only the inputs the platform can supply (its `features.py` lists them).

Two cautions:

- The platform monitors the same patients the models were trained on. What the console shows for them is not a
  test of accuracy; report accuracy from a held-out split in the plug-in's `metrics`.
- `rpm-eval models` reports what each plug-in says about itself. The platform does not re-measure model accuracy.

## 2. Alert latency

Each hop stamps a time, so latency is read from stored data rather than estimated.

| Path | Median | 95th percentile | Note |
|---|---|---|---|
| Queue to scored (pipeline) | 0.01 s | 0.05 s | 4,337 windows in 30 minutes |
| Sensor reading to scored | 0.013 s | 0.05 s | Local broker; over the internet to AWS IoT Core the uplink adds to this |
| Sensor reading to console page | 0.30 s | 5.9 s | Includes a 0.25 s settle so one page carries the final severity |
| Sensor reading to e-mail (Amazon SES) | 1.8 s | 8.2 s | One message per recipient; unverified recipients are tried and refused |
| Alert to agents' summary | 59 s | 62 s | Four agents on Bedrock; does not delay the page |

The safety-relevant figure is sensor to page, and it never depends on the agents. The 95th percentiles for pages are
from only six event-driven pages, two of which were raised while the Care API was being restarted during testing;
the undisturbed end-to-end check measured 0.31 s. More pages are needed before the tail can be trusted.

## 3. Alert quality

Method: the real simulator and the real stream processor run in memory on a virtual clock for a simulated 24 hours
with **all 263 patients in the training data** (243 at home, 20 shown in ICU). This offline evaluation is the only
place where recorded readings are played back: each home patient is placed 2 to 24 hours into their recorded data so
that the rules can be scored against the data's labels. The running platform never streams recorded readings; it
loads them as history and generates new readings from the last one. An alert stays open while the patient
keeps producing alert-worthy readings and is resolved after 30 quiet minutes, so one long deterioration counts as
one alert.

### Against the training data's own labels

Each synthetic patient's wearable stream carries a severity fraction that the platform never sees. It gives
patient-level ground truth for the replayed day.

| What the patient's data says | Patients | Alerted | Alerts per patient per day |
|---|---|---|---|
| Deteriorates (severity reaches 0.5) | 56 | 55 (98%) | 1.05 |
| Mild (severity above 0, below 0.5) | 6 | 0 | 0 |
| Stable throughout | 116 | 0 | 0 |
| MIMIC patients (no severity label) | 37 | 19 | 1.9 |

No stable patient alerted, and all but one deteriorating patient did. The MIMIC patients at home have only five
snapshots each and no severity label, so their alerts cannot be scored as right or wrong; several were discharged
with abnormal vital signs, which is what the rules are reacting to.

For the seven deteriorations that begin during the replayed day (the rest were already under way when it starts),
all seven were detected, a median of 24 minutes after the data's severity first reached 0.5. That is slow, and it is
by design: at home a rise in NEWS2 must persist for 10 minutes before it alerts, and the wearable reports SpO2 and
respiratory rate every 5 minutes. Shortening either trades alert volume for speed.

### Injected scenarios

30 deteriorations and 10 harmless disturbances were injected at known times: home scenarios only into patients whose
own data is stable, ICU scenarios into admitted patients.

| Setting | Scenario | Cases | Detected | Median time to detect |
|---|---|---|---|---|
| Home | Sepsis | 3 | 3 | 6.7 min |
| Home | Heart failure decompensation | 3 | 3 | 11.2 min |
| Home | COPD exacerbation | 3 | 3 | 7.4 min |
| Home | Hypoxia | 3 | 3 | 4.9 min |
| Home | Atrial fibrillation, fast | 3 | 3 | 1.8 min |
| Home | Bradycardia | 3 | 3 | 2.7 min |
| Home | Motion artefact (must not alert) | 5 | 0 alerts | |
| Home | Connectivity loss (must not alert) | 5 | 0 alerts | |
| ICU | Six scenario types, 2 each | 12 | 11 | 1.9 to 9.5 min |

Time to detect is measured from the start of a gradual onset, so it includes the time the illness takes to develop.
The one miss is a fast atrial fibrillation in an ICU patient whose heart rate stayed under the ICU threshold.

### Should the risk models confirm alerts?

A wearable measures only part of NEWS2, so we tried holding a home patient's medium finding until a risk model
agreed (recovery monitoring or early deterioration at or above its alert threshold), scoring the models in the
replay exactly as the risk service does. Same patients, same day, three ways:

| | Rules alone (deployed) | Models confirm a partial NEWS2 | Models confirm every medium finding |
|---|---|---|---|
| Patients whose data deteriorates, alerted | 55 of 56 | 53 of 56 | 53 of 56 |
| Injected deteriorations detected | 36 of 37 | 36 of 37 | 33 of 37 |
| Home bradycardia detected | 3 of 3 | 3 of 3 | 0 of 3 |
| Stable patients alerted | 0 of 116 | 0 of 116 | 0 of 116 |
| Home alerts in the day | 148 | 146 | 143 |
| Findings held for the models / confirmed | | 105 / 95 | 5,998 / 5,985 |

On this data the rules raise no alert for a stable patient, so there are no false alarms for the models to filter:
all confirmation removed was real alerts. Gating single vital-sign rules was worse, because the models do not
recognise a slow heart rate in an otherwise stable patient. Model confirmation is therefore switched off
(`RPM_MODEL_CONFIRMED_ALERTS=false`); the models inform the clinicians and the agents. It is worth measuring again on
data that has false alarms. Reproduce with `rpm-eval replay --model-confirmation`.

### Alert burden

| Setting | Alerts per patient per day | Pages per patient per day |
|---|---|---|
| Home | 0.61 | 0.79 |
| ICU | 0.75 | 1.1 |

Both are under one alert per patient per day. The ICU figure is low because an admitted patient's monitor is
centred on their vital signs at discharge, which are near-normal; it says nothing about a real ICU's alarm load.

## 4. The agents

From the runs recorded during testing (`eval/agents.json`):

| Measure | Result |
|---|---|
| Runs audited | 39 (23 with Claude on Bedrock, 16 offline) |
| Citations checked against the guideline index | 96, all valid |
| Summaries whose advice contradicts the alert severity | 0 |
| Policy violations (patient-facing action in ICU, escalation at low concern) | 0 |
| Time per alert run | median 59 s |
| Tokens per alert run | about 34,000 in, 5,000 out |

At list prices for Claude Haiku 4.5 and Sonnet 4.6 that is roughly US$0.13 per alert run (my estimate from the
measured token counts; check current Bedrock pricing). The Care API allows one agent run per patient every 10
minutes unless the alert's severity rises, and starts no agent work for a patient who already has an open alert.

Guardrail red-team (`eval/guardrails.json`): 13 crafted cases, all held. They cover an agent trying to lower
severity, a prompt-injection asking for a downgrade, patient-facing actions for ICU patients, escalation without
grounds, escalation on a suspected artefact, repeated nagging of a patient, invented actions ("call ambulance",
"prescribe"), an invented citation, and an uncited recommendation. One control case confirms a legitimate critical
escalation still goes through.

## 5. Clinical outcome impact

Not measured, and it cannot be with this data. What the replay shows is detection and lead time against labels the
data carries. Whether earlier flags change outcomes needs a prospective study with real patients and clinicians.

## 6. What was verified by hand

- The clinician console in a browser: worklist, alert with the agents' summary and citations, demo controls.
- Alert e-mail delivered through Amazon SES to a verified ISB mailbox, with unverified recipients reported per
  alert.
- The watch app on the Wear OS emulator against the local stack (before the cohort changed; the device contract is
  unchanged): readings, a clinician's message, a check-in answer and an SOS.
- Not yet verified: a physical Galaxy Watch, and the AWS deployment.
