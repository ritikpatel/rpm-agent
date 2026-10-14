"""Features for Harnoor's early-deterioration model, 6-hour horizon (1.0.0).

His model reads 40 columns of the training data. They were computed by the data generator (rpm_synth
feature_windows.py) from a minute-by-minute simulated watch feed, so this file computes each one the same way from
the platform's readings:

- windows over the last 1 to 24 hours: minimum, maximum, drop (max - min), mean heart-rate variability (the watch's
  RMSSD), hours since the last reading, and counts of abnormal readings using the generator's bands;
- change since discharge against the vital signs charted before discharge (`ctx["discharge_vitals"]`);
- the generator's hourly recovery score (modified NEWS2 plus drift from the discharge values) for
  `score_change_1h` and `sudden_score_drop_flag`;
- from the hospital record (`ctx["record"]`): age, the heart rate and blood pressure charted in the ICU, and labs;
- carried forward from the last recorded snapshot (`ctx["last_recorded"]`): steps, active minutes, activity and
  patient-reported scores, which the platform does not stream.

Two adjustments, both because the platform is not the generator:
- Counts of abnormal readings depend on how often a sensor reports. They are rescaled to the generator's cadence
  (heart rate every minute, SpO2 and respiratory rate every 10, temperature every 30, a cuff about once a day), so a
  device reporting twice as often does not look twice as abnormal.
- The model never saw a missing value in training: it treats one as zero. Every gap is filled, in order, from the
  patient's latest reading, their discharge value, then the median of the MIMIC patients in the training data.

Standard library only.
"""

import statistics
from datetime import timedelta

FEATURES = [
    "activity_change_from_discharge", "abnormal_reading_count_24h", "abnormal_count_spo2_24h",
    "symptom_score_change_from_discharge", "hours_since_discharge", "max_respiratory_rate_12h", "active_minutes",
    "abnormal_count_temp_24h", "PTT", "sudden_score_drop_flag", "respiratory_rate_change_from_discharge", "pCO2",
    "min_respiratory_rate_12h", "heart_rate_change_from_discharge", "anchor_age", "max_spo2_3h",
    "min_respiratory_rate_3h", "hours_since_last_wearable_reading", "step_count", "heart_rate",
    "bun_creatinine_ratio", "non_invasive_bp_systolic", "spo2_drop_6h", "spo2_drop_12h", "latest_pain_score",
    "spo2_change_from_discharge", "hours_since_last_rr", "min_sbp_1h", "eGFR", "min_spo2_12h", "score_change_1h",
    "heart_rate_variability_1h", "multi_vital_worsening_count_1h", "heart_rate_variability_6h", "bicarbonate",
    "activity_std_6h", "heart_rate_variability_12h", "potassium", "pO2", "max_respiratory_rate_6h",
]

COL = {"hr": "heart_rate", "spo2": "spo2", "rr": "resp_rate", "sbp": "sbp", "temp": "temp", "hrv": "hrv_rmssd"}
# the generator's "abnormal" bands (config.ABNORMAL) and the minutes between its sensor samples
ABNORMAL = {"hr": (50, 110), "spo2": (92, 101), "rr": (12, 20), "sbp": (100, 160), "temp": (36.0, 37.8)}
PERIOD_MIN = {"hr": 1.0, "spo2": 10.0, "rr": 10.0, "temp": 30.0, "sbp": 960.0}
WORSENING_DELTA = {"hr": 5.0, "spo2": -1.5, "rr": 2.0, "temp": 0.3}
DRIFT_WEIGHTS, TEMP_DEADBAND = {"hr": 0.30, "spo2": 2.0, "rr": 1.6, "temp": 7.0}, 0.25
# medians of the 63 MIMIC patients in the training data: the last resort for a missing value
MEDIAN = {"PTT": 28.9, "pCO2": 38.5, "pO2": 89.5, "bicarbonate": 26.0, "potassium": 4.1, "eGFR": 94.3,
          "bun_creatinine_ratio": 20.0, "heart_rate": 85.0, "non_invasive_bp_systolic": 124.0, "anchor_age": 62.0,
          "respiratory_rate": 21.0, "spo2": 96.0, "temp": 37.0, "sbp": 124.0,
          "step_count": 636.0, "active_minutes": 0.0, "activity_change_from_discharge": -0.8, "activity_std_6h": 2.8,
          "latest_pain_score": 5.0, "symptom_score_change_from_discharge": 0.0,
          "heart_rate_variability_1h": 18.7, "heart_rate_variability_6h": 20.1, "heart_rate_variability_12h": 20.9}
RECORD = ("PTT", "pCO2", "pO2", "bicarbonate", "potassium", "eGFR", "bun_creatinine_ratio", "heart_rate",
          "non_invasive_bp_systolic", "anchor_age")
CARRIED = ("step_count", "active_minutes", "activity_change_from_discharge", "activity_std_6h", "latest_pain_score",
           "symptom_score_change_from_discharge")


def _series(rows, key):
    return [(r["ts"], float(r[COL[key]])) for r in rows if r.get(COL[key]) is not None]


def _window(series, now, hours, offset_hours=0.0):
    hi = now - timedelta(hours=offset_hours)
    return [v for t, v in series if hi - timedelta(hours=hours) < t <= hi]


def _abnormal_count(series, now, key, hours=24):
    """Abnormal readings in the window, rescaled to the generator's sampling interval for this sensor."""
    times = [t for t, _ in series if t > now - timedelta(hours=hours)]
    values = _window(series, now, hours)
    lo, hi = ABNORMAL[key]
    n = sum(1 for v in values if v < lo or v > hi)
    if n == 0 or len(times) < 2:
        return float(n)
    step = statistics.median((b - a).total_seconds() / 60 for a, b in zip(times, times[1:], strict=False))
    return n * min(step / PERIOD_MIN[key], 1.0)


def _news2(rr, spo2, sbp, hr, temp):
    """The generator's modified NEWS2 (wearable_sim.news2_row): missing parts score 0."""
    s = 0
    if rr is not None:
        s += 3 if rr <= 8 else 1 if rr <= 11 else 0 if rr <= 20 else 2 if rr <= 24 else 3
    if spo2 is not None:
        s += 3 if spo2 <= 91 else 2 if spo2 <= 93 else 1 if spo2 <= 95 else 0
    if sbp is not None:
        s += 3 if sbp <= 90 else 2 if sbp <= 100 else 1 if sbp <= 110 else 0 if sbp < 220 else 3
    if hr is not None:
        s += 3 if hr <= 40 else 1 if hr <= 50 else 0 if hr <= 90 else 1 if hr <= 110 else 2 if hr <= 130 else 3
    if temp is not None:
        s += 3 if temp <= 35.0 else 1 if temp <= 36.0 else 0 if temp <= 38.0 else 1 if temp <= 39.0 else 2
    return s


def _score_at(series, at, base):
    """The generator's hourly recovery score (feature_windows.hourly_scores) without the activity and symptom
    terms, which the platform has no data for: 100 - 7 x NEWS2 - drift from the discharge values."""
    def hour_mean_or_carry(key, carry_h):
        vals = _window(series[key], at, 1)
        if vals:
            return sum(vals) / len(vals)
        past = [v for t, v in series[key] if at - timedelta(hours=carry_h) < t <= at]
        return past[-1] if past else None
    hr, rr = hour_mean_or_carry("hr", 6), hour_mean_or_carry("rr", 6)
    spo2, temp = hour_mean_or_carry("spo2", 6), hour_mean_or_carry("temp", 6)
    sbp = ([v for t, v in series["sbp"] if at - timedelta(hours=12) < t <= at] or [None])[-1]
    drift = 0.0
    for key, cur, worse_when_higher in (("hr", hr, True), ("rr", rr, True), ("spo2", spo2, False),
                                        ("temp", temp, True)):
        if cur is None or base.get(key) is None:
            continue
        d = (cur - base[key]) if worse_when_higher else (base[key] - cur)
        if key == "temp":
            d -= TEMP_DEADBAND
        drift += DRIFT_WEIGHTS[key] * max(d, 0.0)
    return max(0.0, min(100.0, 100 - 7 * _news2(rr, spo2, sbp, hr, temp) - drift))


def build(ctx):
    now = ctx["now"]
    rows = [r for r in ctx["vitals"] if r["ts"] <= now]
    rec, last = ctx.get("record") or {}, ctx.get("last_recorded") or {}
    dv = ctx.get("discharge_vitals") or {}
    s = {k: _series(rows, k) for k in COL}
    latest = {k: (s[k][-1][1] if s[k] else None) for k in COL}
    base = {"hr": dv.get("heart_rate"), "spo2": dv.get("spo2"), "rr": dv.get("resp_rate"), "temp": dv.get("temp")}

    def filled(key, median_key):
        return latest[key] if latest[key] is not None else base.get(key) or MEDIAN[median_key]

    def agg(key, hours, how, median_key):
        vals = _window(s[key], now, hours)
        if not vals:
            vals = [filled(key, median_key)]
        return {"min": min, "max": max, "drop": lambda v: max(v) - min(v),
                "mean": lambda v: sum(v) / len(v)}[how](vals)

    out = {k: float(rec.get(k, MEDIAN[k])) for k in RECORD}
    out.update({k: float(last.get(k, MEDIAN[k])) for k in CARRIED})
    out["hours_since_discharge"] = float(ctx.get("hours_since_discharge") or 0.0)
    for w in (6, 12):
        out[f"max_respiratory_rate_{w}h"] = agg("rr", w, "max", "respiratory_rate")
        out[f"spo2_drop_{w}h"] = agg("spo2", w, "drop", "spo2")
    out["min_respiratory_rate_3h"] = agg("rr", 3, "min", "respiratory_rate")
    out["min_respiratory_rate_12h"] = agg("rr", 12, "min", "respiratory_rate")
    out["max_spo2_3h"] = agg("spo2", 3, "max", "spo2")
    out["min_spo2_12h"] = agg("spo2", 12, "min", "spo2")
    out["min_sbp_1h"] = agg("sbp", 1, "min", "sbp")
    for w in (1, 6, 12):
        vals = _window(s["hrv"], now, w)
        out[f"heart_rate_variability_{w}h"] = (sum(vals) / len(vals) if vals else
                                               latest["hrv"] if latest["hrv"] is not None else
                                               MEDIAN[f"heart_rate_variability_{w}h"])
    counts = {k: _abnormal_count(s[k], now, k) for k in ABNORMAL}
    out["abnormal_count_spo2_24h"] = counts["spo2"]
    out["abnormal_count_temp_24h"] = counts["temp"]
    out["abnormal_reading_count_24h"] = sum(counts.values())
    worsening = 0
    for key, delta in WORSENING_DELTA.items():
        cur, prev = _window(s[key], now, 1), _window(s[key], now, 1, offset_hours=1)
        if cur and prev:
            d = sum(cur) / len(cur) - sum(prev) / len(prev)
            worsening += (delta > 0 and d >= delta) or (delta < 0 and d <= delta)
    out["multi_vital_worsening_count_1h"] = float(worsening)
    out["hours_since_last_wearable_reading"] = (now - rows[-1]["ts"]).total_seconds() / 3600 if rows else 24.0
    out["hours_since_last_rr"] = (now - s["rr"][-1][0]).total_seconds() / 3600 if s["rr"] else 24.0
    out["heart_rate_change_from_discharge"] = filled("hr", "heart_rate") - (base["hr"] or MEDIAN["heart_rate"])
    out["spo2_change_from_discharge"] = filled("spo2", "spo2") - (base["spo2"] or MEDIAN["spo2"])
    out["respiratory_rate_change_from_discharge"] = (filled("rr", "respiratory_rate")
                                                     - (base["rr"] or MEDIAN["respiratory_rate"]))
    score_base = {k: (v if v is not None else MEDIAN.get({"hr": "heart_rate", "rr": "respiratory_rate"}.get(k, k)))
                  for k, v in base.items()}
    now_score = _score_at(s, now, score_base)
    change_1h = now_score - _score_at(s, now - timedelta(hours=1), score_base)
    change_3h = now_score - _score_at(s, now - timedelta(hours=3), score_base)
    out["score_change_1h"] = round(change_1h, 1)
    out["sudden_score_drop_flag"] = 1.0 if (change_1h <= -10 or change_3h <= -15) else 0.0
    return out
