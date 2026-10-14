# Readmission risk (Tiya), 1.0.0

**What it predicts.** The probability of an unplanned readmission within 30 days, re-estimated as the patient
recovers at home.

**Inputs (20).** From the hospital record: age, sex, admissions in the last year, hospital and ICU length of stay,
comorbidities, high-risk medicines, discharge medicines. Live: latest heart rate, SpO2 and temperature, their change
since discharge, time since the last reading and wearable data completeness. Carried forward from the last recorded
snapshot (the platform receives no activity data): steps, active minutes, activity change since discharge, and the
simulated watch's wear rate.

**Evidence.** 63 labelled admissions, 10 readmissions. On the 16 held-out admissions (3 readmissions): AUROC 0.97,
sensitivity 0.33 at the alert threshold. Tiya's grouped cross-validation is the better measure: AUROC 0.42 on average
(range 0.12 to 0.75), below chance. As her explainer says, do not present the single held-out figure as robust.

**What the care team sees.** The 30-day readmission probability, whether it is above her exploratory alert threshold
(33%), the reasons, and her warning that it is a research prototype, not for patient-care decisions.

**On the platform.** Runs for patients at home who have a hospital record (the MIMIC patients), at discharge, hourly
on new readings and daily. Bands: medium from 0.15, high from 0.329 (Tiya's exploratory threshold). Not validated:
shown as weak evidence; it cannot page anyone.

**Serving matches training.** Built by the platform from the training data, the inputs reproduce Tiya's exactly:
all 63 labelled admissions get the same score at 24 hours (`services/risk-service/tests/test_team_models.py`).
