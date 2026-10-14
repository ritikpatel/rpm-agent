"""Navya's Recovery Monitoring Score model (1.0.0): features and the reading of the result.

A port of her recovery_model.py to the platform's patient context.

- `build` (features_from_readings, assemble_features): the five vital signs at discharge, the latest reading of each,
  the worst values of the last six hours, the change since discharge, and her rule-based recovery score. Gaps are
  filled her way: a missing latest value takes the discharge value, a missing six-hour value takes the latest.
- `explain` (RecoveryModel.score): what the care team sees. Her model gives two numbers, in this order:
  1. the recovery score, 0 to 100: 100 means the vital signs are normal and no worse than at discharge, lower is
     worse; Good recovery from 75, Moderate from 50, Poor below 50; with a trend over 3 hours;
  2. the chance of a deterioration episode within the next 72 hours (the LightGBM model), alert from 0.348.
  No reading for more than 6 hours means "Insufficient data": an old score is never shown as current.

Standard library only.
"""

from datetime import timedelta

VITALS = {"hr": "heart_rate", "rr": "resp_rate", "spo2": "spo2", "sbp": "sbp", "temp": "temp"}
FALLBACK = {"hr": 80.0, "rr": 16.0, "spo2": 97.0, "sbp": 120.0, "temp": 36.8}   # only if a discharge value is absent

# Her score rules (model_meta.json "score_rules"): normal range and severe limit per vital, weights, and the change
# from discharge that counts as a full worsening.
RANGES = {"hr": (51.0, 90.0, 40.0, 131.0), "rr": (12.0, 20.0, 8.0, 25.0), "spo2": (96.0, 100.0, 91.0, 100.0),
          "sbp": (111.0, 219.0, 90.0, 220.0), "temp": (36.1, 38.0, 35.0, 39.1)}
WEIGHTS = {"hr": 0.20, "rr": 0.25, "spo2": 0.25, "sbp": 0.15, "temp": 0.15}
WORSENING_STEP = {"hr": 20.0, "rr": 6.0, "spo2": -4.0, "sbp": -20.0, "temp": 1.0}
ABNORMAL_POINTS, WORSENING_POINTS = 70, 30

FEATURES = (["hours_since_discharge"] + [f"discharge_{v}" for v in VITALS] + [f"latest_{v}" for v in VITALS]
            + ["hr_6h_mean", "hr_6h_max", "rr_6h_max", "spo2_6h_min", "sbp_6h_min", "temp_6h_max"]
            + [f"{v}_change_from_discharge" for v in VITALS] + ["recovery_score", "score_change_from_discharge"])


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def _abnormality(vital, value):
    lo, hi, sev_lo, sev_hi = RANGES[vital]
    if value < lo:
        return _clip((lo - value) / (lo - sev_lo), 0.0, 1.0)
    if value > hi and sev_hi > hi:
        return _clip((value - hi) / (sev_hi - hi), 0.0, 1.0)
    return 0.0


def _worsening(vital, value, baseline):
    return _clip((value - baseline) / WORSENING_STEP[vital], 0.0, 1.0)


def recovery_score(latest, baseline):
    """0-100: 100 - 70 x weighted abnormality - 30 x weighted worsening since discharge (her formula)."""
    abn = sum(WEIGHTS[v] * _abnormality(v, latest[v]) for v in VITALS)
    wor = sum(WEIGHTS[v] * _worsening(v, latest[v], baseline[v]) for v in VITALS)
    return round(_clip(100 - ABNORMAL_POINTS * abn - WORSENING_POINTS * wor, 0.0, 100.0), 1)


def build(ctx):
    now = ctx["now"]
    rows = [r for r in ctx["vitals"] if r["ts"] <= now]                      # oldest first
    six = [r for r in rows if r["ts"] > now - timedelta(hours=6)]
    discharge = ctx.get("discharge_vitals") or {}
    base = {v: float(discharge[col]) if discharge.get(col) is not None else FALLBACK[v] for v, col in VITALS.items()}

    def values(subset, v):
        return [float(r[VITALS[v]]) for r in subset if r.get(VITALS[v]) is not None]

    latest = {v: (values(rows, v) or [base[v]])[-1] for v in VITALS}
    hr6 = values(six, "hr")
    worst = {"hr_6h_mean": sum(hr6) / len(hr6) if hr6 else latest["hr"],
             "hr_6h_max": max(hr6) if hr6 else latest["hr"],
             "rr_6h_max": max(values(six, "rr") or [latest["rr"]]),
             "spo2_6h_min": min(values(six, "spo2") or [latest["spo2"]]),
             "sbp_6h_min": min(values(six, "sbp") or [latest["sbp"]]),
             "temp_6h_max": max(values(six, "temp") or [latest["temp"]])}
    score_now, score_at_discharge = recovery_score(latest, base), recovery_score(base, base)
    out = {"hours_since_discharge": float(ctx.get("hours_since_discharge") or 0.0)}
    out.update({f"discharge_{v}": base[v] for v in VITALS})
    out.update({f"latest_{v}": latest[v] for v in VITALS})
    out.update(worst)
    out.update({f"{v}_change_from_discharge": latest[v] - base[v] for v in VITALS})
    out["recovery_score"] = score_now
    out["score_change_from_discharge"] = round(score_now - score_at_discharge, 1)
    return out


# ---------------------------------------------------------------------------------------------- the result, her way
ALERT_THRESHOLD = 0.348
STATUS_BANDS = ((75, "Good recovery", "good"), (50, "Moderate recovery", "warn"), (0, "Poor recovery", "bad"))
NAMES = {"hr": "heart rate", "rr": "respiratory rate", "spo2": "oxygen saturation", "sbp": "blood pressure",
         "temp": "temperature"}
UNITS = {"hr": "bpm", "rr": "/min", "spo2": "%", "sbp": "mmHg", "temp": "C"}
LABELS = {**{f"discharge_{v}": (f"{NAMES[v].capitalize()} at discharge", UNITS[v]) for v in VITALS},
          **{f"latest_{v}": (f"Current {NAMES[v]}", UNITS[v]) for v in VITALS},
          **{f"{v}_change_from_discharge": (f"{NAMES[v].capitalize()} change since discharge", UNITS[v])
             for v in VITALS},
          "hr_6h_mean": ("Average heart rate (6h)", "bpm"), "hr_6h_max": ("Highest heart rate (6h)", "bpm"),
          "rr_6h_max": ("Highest respiratory rate (6h)", "/min"), "spo2_6h_min": ("Lowest oxygen saturation (6h)", "%"),
          "sbp_6h_min": ("Lowest blood pressure (6h)", "mmHg"), "temp_6h_max": ("Highest temperature (6h)", "C"),
          "recovery_score": ("Recovery score", "pts"),
          "score_change_from_discharge": ("Recovery score change since discharge", "pts")}
VITAL_OF = {**{f"latest_{v}": v for v in VITALS}, **{f"{v}_change_from_discharge": v for v in VITALS},
            "hr_6h_mean": "hr", "hr_6h_max": "hr", "rr_6h_max": "rr", "spo2_6h_min": "spo2", "sbp_6h_min": "sbp",
            "temp_6h_max": "temp"}


def _status(score):
    return next((name, tone) for cut, name, tone in STATUS_BANDS if score >= cut)


def _score_at(ctx, hours_ago):
    """Her score_at: the recovery score `hours_ago` hours earlier; before discharge it is the discharge score."""
    hours = float(ctx.get("hours_since_discharge") or 0.0)
    if hours - hours_ago < 0:
        base = build(ctx)
        return recovery_score({v: base[f"discharge_{v}"] for v in VITALS}, {v: base[f"discharge_{v}"] for v in VITALS})
    earlier = dict(ctx, now=ctx["now"] - timedelta(hours=hours_ago), hours_since_discharge=hours - hours_ago)
    return build(earlier)["recovery_score"]


def explain(ctx, features, probability, contributions):
    now = ctx["now"]
    rows = [r for r in ctx["vitals"] if r["ts"] <= now and any(r.get(c) is not None for c in VITALS.values())]
    gap_h = (now - rows[-1]["ts"]).total_seconds() / 3600 if rows else None
    freshness = "Missing" if gap_h is None or gap_h > 6 else "Recent" if gap_h <= 1 else "Stale"
    if freshness == "Missing":
        return {"headline": {"label": "Recovery score", "value": None, "suffix": "/ 100", "status": "Insufficient data",
                             "tone": "unknown"},
                "badge": "Recovery: no data",
                "details": ["No readings for more than 6 hours: no score until data arrives."],
                "concerns": [], "reassurances": []}

    score = features["recovery_score"]
    status, tone = _status(score)
    ch1, ch3, ch24 = (round(score - _score_at(ctx, h), 1) for h in (1, 3, 24))
    trend = "Worsening" if ch3 <= -5 else "Improving" if ch3 >= 5 else "Stable"
    imputed = {v for v, col in VITALS.items() if not any(r.get(col) is not None for r in rows)}

    def described(f):
        label, unit = LABELS[f]
        return f"{label} ({features[f]:.1f} {unit})"
    shown = [f for f in contributions if f != "hours_since_discharge" and VITAL_OF.get(f) not in imputed]
    up = sorted((f for f in shown if contributions[f] > 0.01), key=lambda f: -contributions[f])[:3]
    down = sorted((f for f in shown if contributions[f] < -0.01), key=lambda f: contributions[f])[:3]
    details = [f"Trend over 3 hours: {trend} ({ch3:+.1f} points; last hour {ch1:+.1f}, last 24 hours {ch24:+.1f})"]
    if ch3 <= -10:
        details.append("Sudden decline: the score fell 10 points or more in 3 hours.")
    details.append(f"Chance of a deterioration episode within 72 hours: {probability:.0%} "
                   f"({'at or above' if probability >= ALERT_THRESHOLD else 'below'} the "
                   f"{ALERT_THRESHOLD:.0%} alert threshold).")
    details.append(f"Data: {freshness.lower()}" + (f"; {', '.join(NAMES[v] for v in sorted(imputed))} not measured "
                                                   "yet, taken from discharge" if imputed else "") + ".")
    return {"headline": {"label": "Recovery score", "value": round(score, 1), "suffix": "/ 100", "status": status,
                         "tone": tone, "trend": trend},
            "badge": f"Recovery {score:.0f} · {status.split()[0]}",
            "details": details,
            "concerns": [described(f) for f in up], "reassurances": [described(f) for f in down],
            "values": {"recovery_score": round(score, 1), "recovery_status": status, "trend_direction": trend,
                       "score_change_1h": ch1, "score_change_3h": ch3, "score_change_24h": ch24,
                       "sudden_decline_flag": ch3 <= -10, "poor_recovery_probability": round(probability, 3),
                       "poor_recovery_alert": probability >= ALERT_THRESHOLD, "data_freshness": freshness,
                       "vitals_imputed_from_discharge": len(imputed)}}
