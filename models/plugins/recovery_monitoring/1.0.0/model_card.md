# Recovery monitoring (Navya), 1.0.0

**What it gives the care team (her words: "is the patient recovering, stable or getting worse right now?").**
1. The **recovery score**, 0 to 100: 100 means the vital signs are normal and no worse than at discharge; the lower,
   the worse. Good recovery from 75, Moderate from 50, Poor below 50. With a trend over 3 hours (Improving, Stable,
   Worsening) and a sudden-decline flag. This is a written rule, not a trained model.
2. The **chance of a deterioration episode within the next 72 hours**, from the LightGBM model; alert from 0.348.
   The platform's band (low, medium, high) follows this number.
3. The top three reasons, in her wording. No reading for more than 6 hours shows "Insufficient data", never an old
   score.

The console leads with the recovery score. `explain()` in `features.py` is a port of her `RecoveryModel.score()`; on
her own example request it reproduces her output field for field.

**Inputs (24).** Hours since discharge; heart rate, respiratory rate, SpO2, systolic pressure and temperature at
discharge; the latest value of each; the worst values of the last six hours; the change of each since discharge; the
recovery score and its change since discharge. All come from the live readings and the discharge vital signs, so the
platform can build every one of them.

**Evidence.** Held-out test patients: AUROC 0.91, AUPRC 0.84 (330 snapshots, 86 events); 5-fold cross-validation
AUROC 0.87 (0.82 to 0.94). The recovery score alone reaches AUROC 0.83.

**On the platform.** Runs for patients at home on new readings (at most every 15 minutes) and daily. Bands: medium
from 0.2, high from 0.348 (Navya's alert threshold). Marked validated: a high score can raise an alert.

**Limits.** The outcome comes from the data generator's danger rules, not observed events. Trained on the first 24
hours after discharge. The feature code is a direct port of her `recovery_model.py`; the parity test in
`services/risk-service/tests/test_team_models.py` checks it against her training table.
