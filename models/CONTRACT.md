# Model plug-in contract

This is what Harnoor, Tiya and Navya hand over, and what the risk service expects. A model is one folder:

```
models/plugins/<name>/<version>/
  model.txt         a LightGBM booster in LightGBM's text format (preferred), or
  model.joblib      a fitted model with predict_proba, or model.onnx
  features.py       FEATURES = [...]  and  build(ctx) -> {feature: number}
  metadata.json     what the model is, when it runs, its thresholds and metrics
  model_card.md     a short human description
```

`model.txt` is what `booster.save_model("model.txt")` writes. It loads with any LightGBM 4.x and, unlike a pickle,
cannot run code when it is loaded. Its feature names must equal `FEATURES`, in order, or the plug-in is rejected.
For a LightGBM model the console and the agents get LightGBM's own explanation of each score (its feature
contributions); for other formats, give `feature_means` in the metadata.

Drop the folder in, run `make model-check`, and the risk service serves the newest version of each model. No
platform code changes.

## features.py

```python
FEATURES = ["age", "hr_mean", ...]     # the columns, in the order the model was fitted on

def build(ctx: dict) -> dict:           # one row of features for one patient, right now
    ...
    return {"age": 71.0, "hr_mean": 84.2, ...}
```

Rules:

- Return every name in `FEATURES`, as finite numbers. Impute missing values inside `build`; NaN is rejected.
  This matters for LightGBM: a feature that was never missing in training turns a missing value into 0 at
  prediction time, which reads as "no steps" or "no medicines", not "unknown".
- Import only the Python standard library, numpy and pandas. Do not import platform code, and do not read files or
  call the network: `ctx` is the only input.
- Use this same file to build the training table. That is what keeps training and serving identical.

### What `ctx` contains

| Key | Content |
|---|---|
| `now` | Current time (timezone-aware `datetime`). |
| `care_setting` | `"icu"` or `"home"`. |
| `profile` | `age`, `sex` (`"M"`/`"F"`), `primary_condition` (CHF, COPD, IHD, Sepsis, Stroke), `los_days`, `prior_admissions_12m`, `drg_severity`, `discharge_disposition`, `discharge_at`, `icu_admit_at`, `hypercapnic_resp_failure`, `on_supplemental_o2`. |
| `vitals` | Rows from the last 24 hours, oldest first (at most 3,000). Each has `ts`, `heart_rate`, `hrv_rmssd`, `spo2`, `resp_rate`, `sbp`, `dbp`, `temp`, `skin_temp`, `gcs`, `steps`, `activity`, `news2`, `news2_partial`, `context`. Any value can be `None`: a watch does not measure blood pressure. |
| `daily` | One row per day for the last 7 days, oldest first: `day`, `windows`, `hr_mean`, `hr_min`, `hr_max`, `spo2_mean`, `spo2_min`, `rr_max`, `temp_max`, `hrv_mean`, `steps`, `news2_max`, `news2_mean`. |
| `labs` | Lab results from the last 7 days, oldest first: `ts`, `code`, `name`, `value`, `unit`, `flag`. |
| `hours_since_discharge` | For home patients, otherwise `None`. |
| `record` | Facts from the hospital record, by their names in the training data: `age`, `anchor_age`, admissions in the last 90 and 365 days, hospital and ICU length of stay, comorbidity and chronic-condition counts, high-risk medicines, discharge medicine counts, `BMI`, DRG, the vital signs charted before discharge (`heart_rate`, `respiratory_rate`, `O2_saturation_pulseoxymetry`, `non_invasive_bp_systolic`, `non_invasive_bp_diastolic`, `temperature`) and the labs (`creatinine`, `BUN`, `bun_creatinine_ratio`, `PTT`, `pCO2`, `pO2`, `eGFR` and the rest). Empty for synthetic patients, who have no hospital record. The list is `RECORD_FACTS` in `libs/rpm_core/src/rpm_core/cohort/training.py`. |
| `last_recorded` | Values the platform does not stream, at the patient's last recorded snapshot: `step_count`, `active_minutes`, `latest_activity_level`, `activity_change_from_discharge`, `activity_std_*`, `latest_pain_score`, `latest_symptom_score`, `symptom_score_change_from_discharge`, `wearable_adherence`, `wearable_data_completness`. Empty for synthetic patients. |
| `discharge_vitals` | `heart_rate`, `resp_rate`, `spo2`, `sbp`, `temp` at discharge: charted in the ICU for MIMIC patients, the first point of the wearable stream for synthetic ones. |
| `hours_in_icu` | For ICU patients, otherwise `None`. |

`pd.DataFrame(ctx["vitals"])` gives a table if you prefer pandas. If your model needs something that is not here,
say so: adding a field to the context is a small change on the platform side.

## metadata.json

```json
{
  "name": "early_deterioration",
  "version": "1.0.0",
  "title": "Early deterioration",
  "outcome": "Clinical deterioration needing escalation within about 6 hours",
  "owner": "Harnoor",
  "triggers": ["vitals", "lab"],
  "care_settings": ["icu", "home"],
  "bands": {"medium": 0.2, "high": 0.5},
  "min_interval_s": 60,
  "validated": true,
  "metrics": {"auroc": 0.84, "auprc": 0.41, "n": 5200, "positives": 310},
  "feature_means": {"age": 68.2, "hr_mean": 82.0},
  "libraries": {"scikit-learn": "1.7.2", "xgboost": "3.0.0"},
  "limitations": "Trained on ICU data only."
}
```

- `triggers`: when the model runs. `vitals` (new readings; at most once per `min_interval_s` per patient), `lab` (a
  new lab result), `discharge` (the patient was just discharged), `daily` (a scheduled run for every home patient).
  Any model can also be run on demand.
- `care_settings`: which patients it applies to.
- `bands`: probability thresholds for the medium and high bands. A model entering its high band raises a
  clinical event; only a validated model can page the care team.
- `validated`: set `true` only with evidence in `metrics`. Unvalidated models are labelled as weak evidence in the
  console and to the agents.
- `requires`: parts of the context the model cannot do without, for example `["record"]` for a model trained only
  on patients with a hospital record. The model is not run for a patient who lacks them.
- `feature_means`: reference values used to explain a score (each feature is swapped for its reference to see how
  much it moved the prediction). Leave a feature out and it is simply not explained.
- `libraries`: the versions the model was fitted with. A pickled model only loads reliably with the same library
  versions, so the risk service image is pinned to these. Exporting to ONNX avoids the problem.

## The patients it runs on

The platform monitors the patients in the team's training data (`Training Data/`) and no others, so a model is
scored on the same people it was trained on. That is right for a demonstration and wrong as evidence: report your
model's accuracy from a held-out split in `metrics`, not from how it looks in the console.

The label columns (`deterioration_next_*`, `trajectory_type`, `readmit_*`, `event_severity_fraction`) are not in
`ctx` and are not anywhere the platform can read at run time.

## The three models

| Model | Owner | Runs on | For |
|---|---|---|---|
| `early_deterioration` | Harnoor | new vitals, new labs | home patients with a hospital record |
| `readmission_risk` | Tiya | discharge, hourly on new vitals, daily | home patients with a hospital record |
| `recovery_monitoring` | Navya | new vitals (every 15 minutes), daily | home patients |

The team handed these over on 2026-10-14; they are version `1.0.0` of each. Replace one by adding a higher version
folder next to it (for example `1.1.0`); the older one stops being served. Each folder's `model_card.md` gives the
evidence and the limits, and `source_metadata.json` keeps the owner's own metadata.

## Checking a plug-in

```bash
make model-check
```

It loads every plug-in, builds features for sample ICU and home patients (including one with no data at all),
checks that every feature is present and finite, and scores them. Fitted model files are not committed to git;
It also checks that the platform builds the same inputs each model was trained on, from the shared training data
(`services/risk-service/tests/test_team_models.py`). Model files are committed; deployment publishes the folder
to S3.
