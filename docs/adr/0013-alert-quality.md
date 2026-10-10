# ADR 0013: Alert quality: context-aware rules, tuned by deterministic replay

**Status:** accepted · 2026-09-25 (refines ADR 0001 and ADR 0004)

## Context

Alarm fatigue is the main clinical risk of remote monitoring: a system that pages too often gets ignored. The
week-3 rules (absolute NEWS2 bands, fixed HR limits) produced 5.7 alerts per patient-day at home and 24.6 per ICU
bed-day. Their PPV was 3% and 1.5%, measured against injected ground truth.

## Decision

**1. Measure before tuning.** `rpm-eval replay` runs the production simulator, ICU gateway and stream-engine code
in memory on a virtual clock. It covers 24 h of the 149-device cohort with scheduled deterioration scenarios
(ground truth), negative scenarios (motion artefact, device offline), and a simulated clinician who resolves alerts
after 30 minutes. It is deterministic, so rule sets can be compared on the same data.

**2. Rules use the context the devices already send:**

- **Heart rate is judged against activity:** +10/+25/+35 bpm allowance for light, moderate and vigorous exertion,
  and −5 bpm during sleep. AF with rapid ventricular response still clears these.
- **NEWS2 bands are alerted only on rest windows.** NEWS2 is defined for observations taken at rest; exertional
  windows are stored and shown but not alerted on.
- **Personal baseline:**
  - Once there is 1 h of data, a band alert needs a rise of at least 3 over the patient's 4-h median (the last
    15 min excluded).
  - A red parameter alerts only if it is new, meaning present in less than 20% of the baseline.
  - NEWS2 ≥ 9 always alerts.
  - This follows RCP guidance on established baselines in chronic disease, and it is what makes ICU alerts mean
    *change*.
- **Persistence:** a NEWS2 step must hold for 10 min at home (sparse patient-entered readings) or 3 min in ICU.
- **In ICU, NEWS2 rising ≥ 3 over baseline is alert-worthy.** At home it is informational.
- **Bedside gateway `EDGE_ALERT`s are recorded, not paged.** The bedside alarm has already sounded locally, and the
  cloud scores the same telemetry with patient context, so paging again would double every ICU alert.

The dedicated SpO₂ and HR rules keep their own thresholds and fire independently: they are the fast path.

**3. Choose settings by sweep, not by eye.** The rise and persistence values were picked from a sweep in which
sensitivity and time-to-detect stayed flat while burden fell (`eval/results/sweep/`).

## Result (identical 24-h workload, `eval/results/replay-{baseline,tuned}.json`)

| | Week 3 | Week 4 |
|---|---|---|
| Home alerts per patient-day | 5.71 | **0.66** |
| ICU alerts per bed-day | 24.56 | **4.28** |
| PPV (home / ICU) | 0.03 / 0.015 | **0.28 / 0.11** |
| Deterioration scenarios detected | 35/36 | 35/36 |
| Negative scenarios alerting | 0 | 0 |

## Consequences

- **Detection is slower for NEWS2-driven deteriorations:** median +4 min for heart-failure decompensation at home
  and +3 min in ICU. Fast deteriorations (SpO₂, arrhythmia, SOS or fall) are unaffected or still caught within 2
  minutes. This trade-off is intended and is reported openly.
- **One AF case is missed in both versions.** Its rate plateaued at the 120 bpm threshold. A rhythm-irregularity
  rule (HRV plus rate, or Samsung's irregular-rhythm notification) is the follow-up.
- **The scenario generator was corrected.** "Symptomatic bradycardia" was a −36 bpm offset, which for a patient
  with an 89 bpm baseline gave 53 bpm: not bradycardia. It now targets an absolute 38 bpm. The ground truth was
  wrong, not the detector, and both rule sets were re-run on the corrected workload.
- **Rule state per patient grows by a minute-resolution, 4-h NEWS2 history** (about 240 entries) in Redis.
- **Any rule change must re-run the replay** before release (runbook).
