"""Model feature builders shared by offline training and online serving (one implementation -> no train/serve skew).

Inputs are plain dicts so notebooks (pandas rows), the training script (MIMIC tables) and the model-serving service
(Postgres rows) can all call them. Missing vitals are imputed with normal values and the imputation is reported.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass

from .news2 import News2Input, compute_news2

CONDITIONS = ("CHF", "COPD", "IHD", "Sepsis", "Stroke")
NORMAL = {"heart_rate": 75.0, "spo2": 97.0, "resp_rate": 16.0, "sbp": 120.0, "temp": 36.9, "gcs": 15.0, "news2": 0.0}

READMISSION_FEATURES = [
    "age", "sex_male", *(f"cond_{c}" for c in CONDITIONS), "los_days", "prior_admissions_12m", "drg_severity",
    "discharge_home", "hr_mean", "hr_max", "spo2_min", "rr_max", "sbp_min", "temp_max", "news2_max",
]
ICU_MORTALITY_FEATURES = [
    "age", "sex_male", *(f"cond_{c}" for c in CONDITIONS), "hours_in_icu", "hr_mean", "hr_max", "spo2_min",
    "rr_max", "sbp_min", "temp_max", "gcs_min", "news2_max",
]
INSTABILITY_FEATURES = ["news2_now", "news2_delta_1h", "hr_now", "hr_slope_per_h", "spo2_min_1h", "rr_now",
                        "partial_news2"]


@dataclass
class VitalsAggregate:
    hr_mean: float | None = None
    hr_max: float | None = None
    spo2_min: float | None = None
    rr_max: float | None = None
    sbp_min: float | None = None
    temp_max: float | None = None
    gcs_min: float | None = None
    news2_max: float | None = None
    n: int = 0


def _vals(rows: list[Mapping], key: str) -> list[float]:
    return [float(r[key]) for r in rows if r.get(key) is not None]


def aggregate_vitals(rows: Iterable[Mapping]) -> VitalsAggregate:
    """rows: dicts with heart_rate, spo2, resp_rate, sbp, temp, gcs and optionally news2."""
    rows = list(rows)
    hr, spo2, rr = _vals(rows, "heart_rate"), _vals(rows, "spo2"), _vals(rows, "resp_rate")
    sbp, temp, gcs = _vals(rows, "sbp"), _vals(rows, "temp"), _vals(rows, "gcs")
    news2 = _vals(rows, "news2")
    return VitalsAggregate(
        hr_mean=sum(hr) / len(hr) if hr else None, hr_max=max(hr, default=None), spo2_min=min(spo2, default=None),
        rr_max=max(rr, default=None), sbp_min=min(sbp, default=None), temp_max=max(temp, default=None),
        gcs_min=min(gcs, default=None), news2_max=max(news2, default=None), n=len(rows))


def news2_for_row(row: Mapping, hypercapnic: bool = False) -> int:
    """Full/partial NEWS2 for one charted timepoint (used when the source has no precomputed score)."""
    gcs = row.get("gcs")
    return compute_news2(News2Input(
        resp_rate=row.get("resp_rate"), spo2=row.get("spo2"), on_oxygen=False, sbp=row.get("sbp"),
        heart_rate=row.get("heart_rate"), consciousness=None if gcs is None else ("A" if gcs >= 15 else "C"),
        temp=row.get("temp"), hypercapnic_resp_failure=hypercapnic)).total


def _base(profile: Mapping) -> dict[str, float]:
    cond = profile.get("condition") or profile.get("primary_condition")
    return {"age": float(profile.get("age") or 65), "sex_male": 1.0 if profile.get("sex") == "M" else 0.0,
            **{f"cond_{c}": 1.0 if cond == c else 0.0 for c in CONDITIONS}}


def _vitals(agg: VitalsAggregate, keys: list[str]) -> tuple[dict[str, float], list[str]]:
    src = {"hr_mean": (agg.hr_mean, "heart_rate"), "hr_max": (agg.hr_max, "heart_rate"),
           "spo2_min": (agg.spo2_min, "spo2"), "rr_max": (agg.rr_max, "resp_rate"),
           "sbp_min": (agg.sbp_min, "sbp"), "temp_max": (agg.temp_max, "temp"), "gcs_min": (agg.gcs_min, "gcs"),
           "news2_max": (agg.news2_max, "news2")}
    out, imputed = {}, []
    for k in keys:
        value, normal_key = src[k]
        if value is None:
            imputed.append(k)
            value = NORMAL[normal_key]
        out[k] = float(value)
    return out, imputed


def readmission_features(profile: Mapping, agg: VitalsAggregate) -> tuple[dict[str, float], list[str]]:
    vit, imputed = _vitals(agg, ["hr_mean", "hr_max", "spo2_min", "rr_max", "sbp_min", "temp_max", "news2_max"])
    disp = (profile.get("discharge_disposition") or "").upper()
    feats = {**_base(profile), "los_days": float(profile.get("los_days") or 0),
             "prior_admissions_12m": float(profile.get("prior_admissions_12m") or 0),
             "drg_severity": float(profile.get("drg_severity") or 2),
             "discharge_home": 1.0 if disp.startswith("HOME") else 0.0, **vit}
    return {k: feats[k] for k in READMISSION_FEATURES}, imputed


def icu_mortality_features(profile: Mapping, agg: VitalsAggregate, hours_in_icu: float) -> tuple[dict, list[str]]:
    vit, imputed = _vitals(agg, ["hr_mean", "hr_max", "spo2_min", "rr_max", "sbp_min", "temp_max", "gcs_min",
                                 "news2_max"])
    feats = {**_base(profile), "hours_in_icu": float(hours_in_icu), **vit}
    return {k: feats[k] for k in ICU_MORTALITY_FEATURES}, imputed


def instability_features(recent: list[Mapping]) -> tuple[dict[str, float], list[str]]:
    """recent: vitals rows of the last ~60 min, oldest first, each with ts (datetime), heart_rate, spo2, resp_rate,
    news2, news2_partial."""
    imputed = []
    scored = [r for r in recent if r.get("news2") is not None]
    news2_now = float(scored[-1]["news2"]) if scored else 0.0
    news2_first = float(scored[0]["news2"]) if scored else 0.0
    hr = [(r["ts"], float(r["heart_rate"])) for r in recent if r.get("heart_rate") is not None]
    if len(hr) >= 2 and (hr[-1][0] - hr[0][0]).total_seconds() > 0:
        slope = (hr[-1][1] - hr[0][1]) / ((hr[-1][0] - hr[0][0]).total_seconds() / 3600)
    else:
        slope, _ = 0.0, imputed.append("hr_slope_per_h")
    spo2 = _vals(recent, "spo2")
    rr = _vals(recent, "resp_rate")
    if not spo2:
        imputed.append("spo2_min_1h")
    if not rr:
        imputed.append("rr_now")
    feats = {
        "news2_now": news2_now, "news2_delta_1h": news2_now - news2_first,
        "hr_now": hr[-1][1] if hr else NORMAL["heart_rate"], "hr_slope_per_h": max(-120.0, min(120.0, slope)),
        "spo2_min_1h": min(spo2) if spo2 else NORMAL["spo2"], "rr_now": rr[-1] if rr else NORMAL["resp_rate"],
        "partial_news2": 1.0 if (scored and scored[-1].get("news2_partial")) else 0.0,
    }
    return feats, imputed


def as_dict(agg: VitalsAggregate) -> dict:
    return asdict(agg)
