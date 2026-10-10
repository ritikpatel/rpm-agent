"""Deterministic demo cohort shared by the FHIR loader and the simulator.

Both compute the same assignment from the same inputs (no shared database needed):
  * care setting: patients who died in their last admission + a hashed sample are "currently in ICU";
    everyone else was discharged 1-10 days before the anchor and is monitored at home;
  * a per-patient time shift that moves MIMIC's shifted years (21xx) next to the anchor so FHIR history,
    discharge dates and live streams line up;
  * a home baseline taken from the last ICU observations, and the ICU replay series.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .dataset import GCS_ITEMS, VITAL_ITEMS, MimicDataset, parse_ts, to_float

ICU_REPLAY_START_H = 6.0

_FIRST_F = ["Aisha", "Maria", "Mei", "Fatima", "Grace", "Priya", "Elena", "Amara", "Sofia", "Hannah", "Leila",
            "Noura", "Ananya", "Chloe", "Yuki", "Zara"]
_FIRST_M = ["Omar", "James", "Ravi", "Chen", "Mateo", "Samuel", "Yusuf", "Arjun", "Daniel", "Kofi", "Luca",
            "Hassan", "Vikram", "Ethan", "Kenji", "Tariq"]
_LAST = ["Rahman", "Garcia", "Wang", "Khan", "Okafor", "Sharma", "Rossi", "Haddad", "Novak", "Mensah", "Silva",
         "Iyer", "Tanaka", "Murphy", "Nasser", "Kowalski", "Mehta", "Adeyemi", "Fischer", "Costa"]

HOME_DEFAULTS = {"heart_rate": 76.0, "spo2": 96.0, "resp_rate": 16.0, "sbp": 124.0, "dbp": 76.0, "temp": 36.8}


def _h(seed: str, key: str) -> float:
    digest = hashlib.sha256(f"{seed}:{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


@dataclass(frozen=True)
class CohortMember:
    subject_id: str
    patient_id: str
    display_name: str
    sex: str
    age: int
    condition: str
    care_setting: str  # "home" | "icu"
    device_id: str
    index_hadm_id: str
    index_stay_id: str | None
    time_shift: timedelta
    discharge_at: datetime | None
    icu_admit_at: datetime | None
    replay_start_h: float
    hypercapnic: bool
    baseline: dict[str, float] = field(default_factory=dict)
    los_days: float = 0.0
    prior_admissions_12m: int = 0
    drg_severity: int | None = None
    discharge_disposition: str | None = None

    def shift(self, ts: datetime | None) -> datetime | None:
        return None if ts is None else ts + self.time_shift


def icu_series(ds: MimicDataset, stay_id: str) -> dict[str, list[tuple[float, float]]]:
    """Per-metric (hours since ICU intime, value) points, plus GCS total when all components are charted."""
    rows = ds.chartevents_by_stay.get(stay_id, [])
    if not rows:
        return {}
    t0 = min(parse_ts(r["charttime"]) for r in rows)
    series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    gcs_parts: dict[datetime, dict[str, float]] = defaultdict(dict)
    for r in rows:
        ts, val = parse_ts(r["charttime"]), to_float(r["valuenum"])
        if ts is None or val is None:
            continue
        h = (ts - t0).total_seconds() / 3600
        if r["itemid"] in VITAL_ITEMS:
            series[VITAL_ITEMS[r["itemid"]]].append((h, val))
        elif r["itemid"] in GCS_ITEMS:
            lo, hi = GCS_ITEMS[r["itemid"]]
            gcs_parts[ts][r["itemid"]] = _clamp(round(val), lo, hi)
    # GCS components are charted at different times: carry each forward; a never-charted component is
    # assumed normal (its maximum). Documented limitation of the synthetic extract.
    current = {item: float(hi) for item, (_lo, hi) in GCS_ITEMS.items()}
    for ts in sorted(gcs_parts):
        current.update(gcs_parts[ts])
        series["gcs"].append(((ts - t0).total_seconds() / 3600, float(sum(current.values()))))
    return {k: sorted(v) for k, v in series.items()}


def home_baseline(series: dict[str, list[tuple[float, float]]], condition: str) -> dict[str, float]:
    last = {k: v[-1][1] for k, v in series.items() if v}
    b = {k: last.get(k, d) for k, d in HOME_DEFAULTS.items()}
    return {
        "heart_rate": _clamp(b["heart_rate"] - 5, 58, 98),
        "spo2": _clamp(b["spo2"], 89 if condition == "COPD" else 93, 99),
        "resp_rate": _clamp(b["resp_rate"], 12, 20),
        "sbp": _clamp(b["sbp"], 100, 150),
        "dbp": _clamp(b["dbp"], 60, 92),
        "temp": _clamp(b["temp"], 36.3, 37.3),
    }


def _name(seed: str, sid: str, sex: str) -> str:
    firsts = _FIRST_F if sex == "F" else _FIRST_M
    return f"{firsts[int(_h(seed, sid + 'f') * len(firsts))]} {_LAST[int(_h(seed, sid + 'l') * len(_LAST))]}"


def prior_admissions(adms: list[dict], index: dict, days: int = 365) -> int:
    t = parse_ts(index["admittime"])
    return sum(1 for a in adms if a is not index and a["hadm_id"] != index["hadm_id"]
               and parse_ts(a["admittime"]) < t and (t - parse_ts(a["admittime"])).days <= days)


def drg_severity(ds: MimicDataset, hadm_id: str) -> int | None:
    sev = [int(r["drg_severity"]) for r in ds.drg_by_hadm.get(hadm_id, []) if r.get("drg_severity")]
    return max(sev) if sev else None


def build_cohort(ds: MimicDataset, anchor: datetime, icu_count: int = 25, seed: str = "rpm-v1") -> list[CohortMember]:
    index_adm = {sid: adms[-1] for sid, adms in ds.admissions_by_subject.items() if sid in ds.patients}
    died = {sid for sid, a in index_adm.items() if a.get("hospital_expire_flag") == "1"}
    ranked = sorted((sid for sid in index_adm if sid not in died), key=lambda s: _h(seed, "icu:" + s))
    icu = died | set(ranked[: max(0, icu_count - len(died))])

    members: list[CohortMember] = []
    for sid in sorted(index_adm):
        adm, pat = index_adm[sid], ds.patients[sid]
        stays = ds.icustays_by_hadm.get(adm["hadm_id"], [])
        stay = stays[-1] if stays else None
        condition = adm.get("condition") or "UNKNOWN"
        series = icu_series(ds, stay["stay_id"]) if stay else {}
        setting = "icu" if sid in icu and stay else "home"

        if setting == "icu":
            target = anchor - timedelta(hours=ICU_REPLAY_START_H)
            shift = target - parse_ts(stay["intime"])
            discharge_at, icu_admit_at = None, target
        else:
            days = 1 + int(_h(seed, "dc:" + sid) * 10)
            original = parse_ts(adm["dischtime"]) or parse_ts(adm["admittime"])
            target = (anchor - timedelta(days=days)).replace(hour=original.hour or 11, minute=original.minute)
            shift = target - original
            discharge_at, icu_admit_at = target, None

        members.append(CohortMember(
            subject_id=sid,
            patient_id=f"p-{sid}",
            display_name=_name(seed, sid, pat.get("gender", "")),
            sex=pat.get("gender", "U"),
            age=int(pat.get("anchor_age") or 0),
            condition=condition,
            care_setting=setting,
            device_id=f"sim-icu-{sid}" if setting == "icu" else f"sim-{sid}",
            index_hadm_id=adm["hadm_id"],
            index_stay_id=stay["stay_id"] if stay else None,
            time_shift=shift,
            discharge_at=discharge_at,
            icu_admit_at=icu_admit_at,
            replay_start_h=ICU_REPLAY_START_H,
            hypercapnic=condition == "COPD",
            baseline=home_baseline(series, condition),
            los_days=float(adm.get("length_of_stay_days") or 0),
            prior_admissions_12m=prior_admissions(ds.admissions_by_subject[sid], adm),
            drg_severity=drg_severity(ds, adm["hadm_id"]),
            discharge_disposition=None if setting == "icu" else adm.get("discharge_location"),
        ))
    return members


def select_cohort(members: list[CohortMember], icu: int = 0, home: int = 0, seed: str = "rpm-v1") -> list[CohortMember]:
    """A smaller, deterministic slice of the cohort (0 keeps everyone in that setting). The seed tool and the
    simulator both call this with the same numbers, so they agree on who is monitored."""
    def take(setting: str, n: int) -> list[CohortMember]:
        group = sorted((m for m in members if m.care_setting == setting), key=lambda m: _h(seed, "pick:" + m.subject_id))
        return group[:n] if n else group
    chosen = {m.patient_id for m in take("icu", icu) + take("home", home)}
    return [m for m in members if m.patient_id in chosen]


def sim_device_id(patient_id: str, setting: str) -> str:
    """The simulator's device for a patient in a care setting."""
    suffix = patient_id.removeprefix("p-")
    return f"sim-icu-{suffix}" if setting == "icu" else f"sim-{suffix}"


def pick_demo_patient(members: list[CohortMember], requested: str | None) -> CohortMember | None:
    """Patient streamed by the real Galaxy Watch. 'auto' = first home-monitored CHF patient (the storyline's
    high-readmission-risk heart-failure discharge). Loader and simulator both call this, so they agree."""
    if not requested:
        return None
    if requested == "auto":
        return next((m for m in members if m.care_setting == "home" and m.condition == "CHF"), None)
    return next((m for m in members if requested in (m.patient_id, m.subject_id)), None)
