"""Features for Navya's Recovery Monitoring Score model (1.0.0).

A port of her recovery_model.py (features_from_readings, assemble_features, recovery_score) to the platform's patient
context, so the model gets exactly the inputs it was trained on: the five vital signs at discharge, the latest
reading of each, the worst values of the last six hours, the change since discharge, and her rule-based recovery
score. Gaps are filled her way: a missing latest value takes the discharge value, a missing six-hour value takes the
latest. Standard library only.
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
