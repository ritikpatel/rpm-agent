"""Features for the early-deterioration model, 6-hour horizon (1.1.0: retrained on the shared training data).

His model reads 40 columns of the training data. They were computed by the data generator (rpm_synth
feature_windows.py) from a minute-by-minute simulated watch feed, so this file computes each one the same way from
the platform's readings:

- windows over the last 1 to 24 hours: minimum, maximum, drop (max - min), mean heart-rate variability (the watch's
  RMSSD), hours since the last reading, and counts of abnormal readings using the generator's bands;
- change since discharge against the vital signs charted before discharge (`ctx["discharge_vitals"]`);
- the generator's hourly recovery score (modified NEWS2 plus drift from the discharge values) for
  `score_change_1h` and `sudden_score_drop_flag`;
- from the hospital record (`ctx["record"]`): age, the heart rate and blood pressure charted in the ICU, and labs
  (a patient without one gets the age they were registered with and the training medians for the rest);
- carried forward from the last recorded snapshot (`ctx["last_recorded"]`): steps, active minutes, activity and
  patient-reported scores, which the platform does not stream.

Two adjustments, both because the platform is not the generator:
- Counts of abnormal readings depend on how often a sensor reports. They are rescaled to the generator's cadence
  (heart rate every minute, SpO2 and respiratory rate every 10, temperature every 30, a cuff about once a day), so a
  device reporting twice as often does not look twice as abnormal.
- The model never saw a missing value in training: it treats one as zero. Every gap is filled, in order, from the
  patient's latest reading, their discharge value, then the median of the MIMIC patients in the training data.

1.1.0 reads 33 of these: not the six carried forward from the last recorded snapshot (frozen when serving) and
not the hours since discharge (see models/training/early_deterioration/train.py, which builds its training table
with this file). Standard library only.
"""

import statistics
from datetime import timedelta

FEATURES = [
    "abnormal_reading_count_24h", "abnormal_count_spo2_24h", "max_respiratory_rate_12h", "abnormal_count_temp_24h",
    "PTT", "sudden_score_drop_flag", "respiratory_rate_change_from_discharge", "pCO2", "min_respiratory_rate_12h",
    "heart_rate_change_from_discharge", "anchor_age", "max_spo2_3h", "min_respiratory_rate_3h",
    "hours_since_last_wearable_reading", "heart_rate", "bun_creatinine_ratio", "non_invasive_bp_systolic",
    "spo2_drop_6h", "spo2_drop_12h", "spo2_change_from_discharge", "hours_since_last_rr", "min_sbp_1h", "eGFR",
    "min_spo2_12h", "score_change_1h", "heart_rate_variability_1h", "multi_vital_worsening_count_1h",
    "heart_rate_variability_6h", "bicarbonate", "heart_rate_variability_12h", "potassium", "pO2",
    "max_respiratory_rate_6h",
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
    if "anchor_age" not in rec and ctx.get("profile", {}).get("age") is not None:
        out["anchor_age"] = float(ctx["profile"]["age"])
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


# ------------------------------------------------------------------------------------ the result, the author's way
# The author's inference.py: the probability of deterioration in the next 6 hours, an alert level, and up to three
# named drivers (DRIVER_CANDIDATES), each shown with its value over the last 3 hours. The alert threshold keeps the
# author's 70%-recall rule, re-chosen on the retrained model's cross-validated predictions.
HIGH = 0.8431   # caught 70% of deteriorations in grouped cross-validation (train.py)


def _shock_index_max(s, now, hours=3):
    """Heart rate / systolic pressure, pairing each cuff reading with the heart rate within 10 minutes of it."""
    lo = now - timedelta(hours=hours)
    out = []
    for t, sbp in s["sbp"]:
        near = [v for th, v in s["hr"] if abs((th - t).total_seconds()) <= 600]
        if t > lo and near and sbp > 0:
            out.append(sum(near) / len(near) / sbp)
    return max(out) if out else None


def _drivers(ctx):
    now = ctx["now"]
    rows = [r for r in ctx["vitals"] if r["ts"] <= now]
    s = {k: _series(rows, k) for k in COL}

    def win(key, hours=3, offset=0.0):
        return _window(s[key], now, hours, offset)
    spo2, hr, sbp, rr = win("spo2"), win("hr"), win("sbp"), win("rr")
    worsening = 0
    for key, delta in WORSENING_DELTA.items():
        cur, prev = win(key), win(key, 3, 3)
        if cur and prev:
            d = sum(cur) / len(cur) - sum(prev) / len(prev)
            worsening += (delta > 0 and d >= delta) or (delta < 0 and d <= delta)
    values = {"min_spo2_3h": min(spo2) if spo2 else None, "spo2_drop_3h": max(spo2) - min(spo2) if spo2 else None,
              "max_heart_rate_3h": max(hr) if hr else None, "min_sbp_3h": min(sbp) if sbp else None,
              "sbp_drop_3h": max(sbp) - min(sbp) if sbp else None, "max_respiratory_rate_3h": max(rr) if rr else None,
              "shock_index_max_3h": _shock_index_max(s, now),
              "activity_change_from_discharge": (ctx.get("last_recorded") or {}).get("activity_change_from_discharge"),
              "multi_vital_worsening_count_3h": float(worsening)}
    rules = (("min_spo2_3h", "SpO2 dropped", lambda v: v < 92), ("spo2_drop_3h", "SpO2 dropped", lambda v: v > 3),
             ("max_heart_rate_3h", "Heart rate elevated", lambda v: v > 100),
             ("min_sbp_3h", "Blood pressure low", lambda v: v < 90),
             ("sbp_drop_3h", "Blood pressure dropping", lambda v: v > 10),
             ("max_respiratory_rate_3h", "Breathing rate elevated", lambda v: v > 22),
             ("shock_index_max_3h", "Shock index elevated", lambda v: v > 0.9),
             ("activity_change_from_discharge", "Activity reduced vs. baseline", lambda v: v < -15),
             ("multi_vital_worsening_count_3h", "Multiple vitals worsening together", lambda v: v >= 2))
    return [f"{label} ({col}={values[col]:.1f})" for col, label, cond in rules
            if values[col] is not None and cond(values[col])][:3]


def explain(ctx, features, probability, contributions):
    high = probability >= HIGH
    level, tone = ("Above the alert threshold", "bad") if high else ("Below the alert threshold", "good")
    drivers = _drivers(ctx)
    details = [f"Alert threshold: {HIGH:.0%} (caught 70% of deteriorations in cross-validation)."]
    if not ctx.get("record"):
        details.append("Estimated without a hospital record: labs and charted vital signs set to typical values.")
    return {"headline": {"label": "Deterioration in the next 6 hours", "value": f"{probability:.0%}", "suffix": "",
                         "status": level, "tone": tone},
            "badge": f"Deterioration {probability:.0%}",
            "details": details,
            "concerns": drivers or ["No single driver stands out: check the full vital signs."], "reassurances": [],
            "note": "Not validated yet: retrained on the shared training data (cross-validated AUROC 0.98; 0.79 on "
                    "the real MIMIC patients), where no deterioration starts later than about 12 hours after "
                    "discharge, so its use at later hours is untested.",
            "values": {"predicted_probability": round(probability, 4), "alert": high, "top_drivers": drivers,
                       "estimated": not ctx.get("record")}}
