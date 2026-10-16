# Early deterioration, 1.1.0 (retrained on the shared training data)

**What it predicts.** The probability that a deterioration (a vital-sign danger rule sustained for 30 minutes)
starts in the next 6 hours. Same outcome as 1.0.0, the author's hand-over.

**Why it was retrained.** 1.0.0 was fitted on a 300-patient file that is not in the hand-over and ranks the shared
data's patients no better than chance (AUROC 0.51 with its own columns; 0.73 through the platform's inputs). 1.1.0
is refitted on the shared file by `models/training/early_deterioration/train.py`, with every input built by this
plug-in's own `features.py` from the context the risk service builds, so training and serving read the same inputs.

**Inputs (33).** 1.0.0's inputs less seven: the six carried forward from the last recorded snapshot (steps, active
minutes, activity change and variability, pain and symptom scores), which are frozen when serving, and the hours
since discharge. In this data a deterioration only starts within about 12 hours of discharge (none of the 526
snapshots at 12 or 24 hours is labelled), so with that input the model learnt "early means risky" and scored every
patient near zero from 24 hours on, which is when the platform monitors them.

**Evidence.** All 1,315 labelled snapshots (263 patients, 163 deteriorations); 5-fold cross-validation with each
patient in one fold, three seeds, trees chosen inside each training fold.

| On the same rows | AUROC |
|---|---|
| 1.1.0, cross-validated | 0.98 (0.980, 0.981, 0.980 by seed); AUPRC 0.945 |
| 1.1.0, the 63 real MIMIC patients (23 events) | 0.79 |
| 1.1.0, the 200 synthetic patients | 0.998 |
| 1.0.0 | 0.73 |
| NEWS2 alone | 0.875 |

The synthetic rows are partly learning the data generator that made their labels; the MIMIC figure is the more
realistic one. Alert threshold 84% (`bands.high`): in cross-validation it caught 70% of deteriorations with no false
alarm (specificity and PPV 1.0); no stable patient's snapshot was above it.

**At the hours the platform monitors.** Untested on labels: the data has none after 12 hours. Simulated instead
(stable patients from where their recorded data ends, a deterioration injected an hour later, scored every 10
minutes): it crossed its threshold for 3 of 3 sepsis, 2 of 3 heart failure and 1 of 3 COPD cases, none of 3 hypoxia
and none of 6 fast or slow heart rhythms, and never for the 8 patients without one. When it crossed, the rules had
already paged, 2 to 10 minutes earlier. It adds context, not speed.

**On the platform.** Patients at home, on new readings (at most every 5 minutes) and new labs. **Not validated**:
shown with this evidence and to the agents, it cannot page on its own. A deterioration already under way scores low;
the rules alert on it. Patients without a hospital record are scored with the training medians for labs and charted
vital signs, marked as estimated.

**What would validate it.** Labelled data in which deteriorations start at the hours the platform monitors (24
hours after discharge and later), and a held-out result there.
