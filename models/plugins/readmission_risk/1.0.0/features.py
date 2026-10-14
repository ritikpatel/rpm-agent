"""Features for Tiya's 30-day readmission model (1.0.0).

Her model reads one post-discharge snapshot of 20 columns from the training data. This builds the same 20 from the
platform's patient context:

- from the hospital record (`ctx["record"]`): age, prior admissions, length of stay, ICU stay, comorbidities,
  high-risk medicines, discharge medicines;
- from the live readings: the latest heart rate, SpO2 and temperature, their change since discharge, how long since
  the last reading, and how complete the wearable data has been;
- carried forward from the last recorded snapshot (`ctx["last_recorded"]`): steps, active minutes, the change in
  activity since discharge and the watch's wear pattern. The platform receives no activity data, so these are the
  values the record ends with.

The model was fitted on the 63 MIMIC patients only (the only ones with a readmission label), so it runs only for
patients with a hospital record (metadata "requires": ["record"]). Every gap is filled: the model turns a missing
value into zero for most of its inputs, which would read as "no medicines" or "no steps". Standard library only.
"""

from datetime import timedelta

FEATURES = [
    "hours_since_discharge", "age", "gender_male", "prior_admission_count_365d", "hospital_length_of_stay",
    "icu_length_of_stay", "comorbidity_count", "high_risk_med_flag", "discharge_medication_count",
    "latest_heart_rate", "latest_oxygen_saturation", "latest_temperature", "step_count", "active_minutes",
    "hours_since_last_wearable_reading", "wearable_data_completness", "wearable_adherence",
    "heart_rate_change_from_discharge", "spo2_change_from_discharge", "activity_change_from_discharge",
]
# Medians of the 63 MIMIC patients in the training data: used only when a value is missing.
MEDIAN = {"age": 62.0, "prior_admission_count_365d": 0.0, "hospital_length_of_stay": 7.83, "icu_length_of_stay": 2.69,
          "comorbidity_count": 15.0, "high_risk_med_flag": 1.0, "discharge_medication_count": 2.0,
          "step_count": 636.0, "active_minutes": 0.0, "activity_change_from_discharge": -0.8,
          "wearable_adherence": 0.77, "wearable_data_completness": 0.976, "heart_rate": 85.0, "spo2": 96.0,
          "temp": 37.26}
RECORD = ("prior_admission_count_365d", "hospital_length_of_stay", "icu_length_of_stay", "comorbidity_count",
          "high_risk_med_flag", "discharge_medication_count")
CARRIED = ("step_count", "active_minutes", "activity_change_from_discharge", "wearable_adherence")


def _latest(rows, key):
    vals = [float(r[key]) for r in rows if r.get(key) is not None]
    return vals[-1] if vals else None


def _completeness(rows, now, fallback):
    """Share of minutes with a heart-rate reading, over the live readings of the last 24 hours (the training data
    measured it the same way on the simulated watch). Until an hour of live data exists, the recorded value."""
    live = [r["ts"] for r in rows if r.get("source") != "record" and r.get("heart_rate") is not None
            and r["ts"] > now - timedelta(hours=24)]
    if not live or (now - live[0]).total_seconds() < 3600:
        return fallback
    minutes = {t.replace(second=0, microsecond=0) for t in live}
    return min(len(minutes) / ((now - live[0]).total_seconds() / 60), 1.0)


def build(ctx):
    now = ctx["now"]
    rows = [r for r in ctx["vitals"] if r["ts"] <= now]
    rec, last = ctx.get("record") or {}, ctx.get("last_recorded") or {}
    base = ctx.get("discharge_vitals") or {}
    out = {"hours_since_discharge": float(ctx.get("hours_since_discharge") or 0.0),
           "age": float(rec.get("age", ctx["profile"].get("age") or MEDIAN["age"])),
           "gender_male": 1.0 if str(ctx["profile"].get("sex", "")).upper().startswith("M") else 0.0}
    out.update({k: float(rec.get(k, MEDIAN[k])) for k in RECORD})
    hr = _latest(rows, "heart_rate") or base.get("heart_rate") or MEDIAN["heart_rate"]
    spo2 = _latest(rows, "spo2") or base.get("spo2") or MEDIAN["spo2"]
    temp = _latest(rows, "temp") or base.get("temp") or MEDIAN["temp"]
    out.update({"latest_heart_rate": hr, "latest_oxygen_saturation": spo2, "latest_temperature": temp})
    out.update({k: float(last.get(k, MEDIAN[k])) for k in CARRIED})
    out["hours_since_last_wearable_reading"] = (now - rows[-1]["ts"]).total_seconds() / 3600 if rows else 24.0
    out["wearable_data_completness"] = _completeness(
        rows, now, float(last.get("wearable_data_completness", MEDIAN["wearable_data_completness"])))
    out["heart_rate_change_from_discharge"] = hr - float(base.get("heart_rate", hr))
    out["spo2_change_from_discharge"] = spo2 - float(base.get("spo2", spo2))
    return out
