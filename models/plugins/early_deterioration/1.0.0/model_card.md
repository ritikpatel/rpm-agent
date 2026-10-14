# Early deterioration (Harnoor), 1.0.0

**What it predicts.** The probability of deterioration (a vital-sign danger rule sustained for 30 minutes) in the
next 6 hours. Harnoor also trained a 3-hour model; it is not served (lower precision, and its two thresholds are
almost equal).

**What the care team sees.** The probability of deterioration in the next 6 hours, a level (High from 27%, Watch
from 0.9%, from his two thresholds in their natural order) and up to three of his named drivers, for example "SpO2
dropped (min_spo2_3h=88.0)".

**Inputs (40).** Windows over the last 1 to 24 hours of respiratory rate, SpO2, blood pressure and heart-rate
variability; counts of abnormal readings; change since discharge; the generator's hourly recovery score; time since
discharge and since the last readings; labs and vital signs from the hospital record; steps, activity and
patient-reported scores carried forward from the last recorded snapshot.

**Evidence, and a problem.** Harnoor reports AUROC 0.92 and AUPRC 0.63 on his own test split, of a 300-patient file
that is not in the hand-over. Scored on the shared training data with the model's own columns, it reaches AUROC 0.51
(0.64 on the MIMIC rows alone): no better than chance. It also never saw a missing value, so every gap must be
filled; through the platform's feature builder, which does that, AUROC on the MIMIC snapshots is 0.75 (23 events
in 315). Weak, and two thirds of snapshots fall in the medium band.

**On the platform.** Runs for patients at home with a hospital record, on new readings (at most every 5 minutes) and
new labs. ICU patients rely on the NEWS2 rules, which alert without any model. Bands: medium from 0.009, high from
0.272 (Harnoor's 90% and 70% recall thresholds). Not validated: shown as weak evidence; it cannot page anyone.

**What would fix it.** Retrain on the shared training data with only the inputs the platform can supply live (see
`features.py`), and report a held-out result on that file.
