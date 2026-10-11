# Evaluation report

Measured on 10 October 2026 on the local stack (Docker on a MacBook, 8 GB) with the patients in the team's training
data. The raw outputs are in [eval/](eval). Every number here can be regenerated with the commands in the
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
| How many alerts does a team get? | 0.61 per patient per day at home, 0.75 in ICU | `eval/replay.json` |
| Are the agents' citations real? | 96 of 96 citations checked resolve to a retrieved passage | `eval/agents.json` |
| Do the guardrails hold? | 13 of 13 red-team cases held | `eval/guardrails.json` |
| How accurate are the risk models? | Not established: the three models are placeholders | `eval/models.json` |

## 1. Prediction accuracy

The three models in `models/plugins/` are placeholders so the platform runs end to end. Their numbers say nothing
about the models the team is training.

| Model | Owner of the real model | Placeholder | Cross-validated AUROC |
|---|---|---|---|
| Early deterioration | Harnoor | Logistic model with hand-set weights | none (not fitted) |
| Readmission risk | Tiya | Logistic regression on the 63 MIMIC patients, 10 readmitted | 0.70 |
| Recovery monitoring | Navya | Logistic model with hand-set weights | none (not fitted) |

An AUROC of 0.70 from 63 discharges with 10 readmissions is too little evidence to act on, so every placeholder is
marked `validated: false` whatever it scores. An unvalidated model is labelled as weak evidence in the console and
to the agents, and cannot page anyone.

Two cautions for the real models:

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
