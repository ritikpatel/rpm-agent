"""Features for the 30-day readmission model (PLACEHOLDER; Tiya's feature module replaces this file).

Self-contained on purpose: a plug-in's feature code imports nothing from the platform."""

CONDITIONS = ("CHF", "COPD", "IHD", "Sepsis", "Stroke")
FEATURES = ["age", "sex_male", *(f"cond_{c}" for c in CONDITIONS), "los_days", "prior_admissions_12m",
            "drg_severity", "discharge_home", "hr_mean", "hr_max", "spo2_min", "rr_max", "sbp_min", "temp_max",
            "news2_max"]
NORMAL = {"hr_mean": 75.0, "hr_max": 75.0, "spo2_min": 97.0, "rr_max": 16.0, "sbp_min": 120.0, "temp_max": 36.9,
          "news2_max": 0.0}


def _vals(rows, key):
    return [float(r[key]) for r in rows if r.get(key) is not None]


def build(ctx: dict) -> dict:
    p, rows = ctx["profile"], ctx["vitals"]
    hr = _vals(rows, "heart_rate")
    agg = {
        "hr_mean": sum(hr) / len(hr) if hr else None,
        "hr_max": max(hr, default=None),
        "spo2_min": min(_vals(rows, "spo2"), default=None),
        "rr_max": max(_vals(rows, "resp_rate"), default=None),
        "sbp_min": min(_vals(rows, "sbp"), default=None),
        "temp_max": max(_vals(rows, "temp"), default=None),
        "news2_max": max(_vals(rows, "news2"), default=None),
    }
    cond = p.get("primary_condition")
    disposition = (p.get("discharge_disposition") or "").upper()
    return {
        "age": float(p.get("age") or 65),
        "sex_male": 1.0 if p.get("sex") == "M" else 0.0,
        **{f"cond_{c}": 1.0 if cond == c else 0.0 for c in CONDITIONS},
        "los_days": float(p.get("los_days") or 0),
        "prior_admissions_12m": float(p.get("prior_admissions_12m") or 0),
        "drg_severity": float(p.get("drg_severity") or 2),
        "discharge_home": 1.0 if disposition.startswith("HOME") else 0.0,
        **{k: (v if v is not None else NORMAL[k]) for k, v in agg.items()},   # impute missing vitals with normals
    }
