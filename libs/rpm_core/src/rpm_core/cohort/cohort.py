"""The monitored cohort, built only from the training data.

Every patient in the data has been discharged, so by default everyone is monitored at home on a wearable, a
deterministic number of hours into their post-discharge course. A few of the real MIMIC patients are shown as still
admitted to the ICU (their data starts at discharge, so they are simply "before hour 0"): a bedside monitor reports
their discharge-time vital signs until they are discharged, and from then on their own post-discharge data plays.

No patient, vital sign or lab value comes from anywhere else. Names are the one invented field: the data has none.
"""

from __future__ import annotations

import bisect
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from .training import VITALS, PatientRecord, TrainingData

_FIRST_F = ["Aisha", "Maria", "Mei", "Fatima", "Grace", "Priya", "Elena", "Amara", "Sofia", "Hannah", "Leila",
            "Chloe", "Ananya", "Yuki", "Zara", "Ruth"]
_FIRST_M = ["Omar", "James", "Ravi", "Chen", "Mateo", "Samuel", "Yusuf", "Arjun", "Daniel", "Kofi", "Luca",
            "Tariq", "Ethan", "Hiro", "Vikram", "Noah"]
_LAST = ["Rahman", "Garcia", "Wang", "Khan", "Okafor", "Sharma", "Rossi", "Haddad", "Novak", "Mensah", "Silva",
         "Iyer", "Kowalski", "Fischer", "Mehta", "Adeyemi", "Murphy", "Tanaka", "Costa", "Bello"]
NORMAL = {"heart_rate": 76.0, "resp_rate": 16.0, "spo2": 97.0, "sbp": 122.0, "dbp": 76.0, "temp": 36.8, "gcs": 15.0}
_CATEGORY = (  # first match wins; `primary_condition` drives condition-specific alert thresholds and check-in wording
    ("Sepsis", r"sepsis|septic"), ("CHF", r"heart failure"), ("COPD", r"obstructive pulmonary|copd|emphysema"),
    ("IHD", r"myocardial infarction|ischemic heart|coronary|angina|nstemi|stemi"),
    ("Stroke", r"cerebral infarction|stroke|intracranial hemorrhage|subarachnoid"),
)


def _h(seed: str, key: str) -> float:
    return int(hashlib.sha256(f"{seed}:{key}".encode()).hexdigest()[:12], 16) / float(1 << 48)


def sim_device_id(patient_id: str, setting: str) -> str:
    """The simulator's device for a patient in a care setting."""
    return f"sim-icu-{patient_id}" if setting == "icu" else f"sim-{patient_id}"


def condition_of(p: PatientRecord) -> str:
    text = (p.text("primary_diagnosis") or "").lower()
    if p.flag("has_sepsis_diagnosis_flag") and re.search(_CATEGORY[0][1], text):
        return "Sepsis"
    for name, pattern in _CATEGORY:
        if re.search(pattern, text):
            return name
    if not p.real:
        return "Post-discharge"
    if p.flag("has_sepsis_diagnosis_flag"):
        return "Sepsis"
    return "Cardiac" if p.flag("has_cardiac_diagnosis_flag") else \
        "Respiratory" if p.flag("has_respiratory_diagnosis_flag") else "Other"


@dataclass(frozen=True)
class CohortMember:
    subject_id: str
    patient_id: str
    display_name: str
    sex: str
    age: int
    condition: str                    # category, see condition_of
    diagnosis: str | None             # primary diagnosis text (MIMIC patients)
    origin: str                       # mimic_real | synthetic
    care_setting: str                 # "home" | "icu"
    device_id: str
    discharge_at: datetime | None
    icu_admit_at: datetime | None
    hypercapnic: bool
    baseline: dict[str, float] = field(default_factory=dict)   # vital signs at discharge (the ICU monitor's centre)
    points: tuple = ()                # ((hours since discharge, {vital: value}), ...) for the wearable
    los_days: float = 0.0
    prior_admissions_12m: int = 0
    drg_severity: int | None = None
    discharge_disposition: str | None = None
    record: PatientRecord | None = field(default=None, compare=False, repr=False)


def trajectory_at(points: tuple | list, hours: float) -> dict[str, float]:
    """The patient's own vital signs `hours` after discharge, linearly interpolated between data points and held at
    the ends. Empty if the data has no usable trajectory."""
    if not points:
        return {}
    xs = [p[0] for p in points]
    i = bisect.bisect_right(xs, hours)
    if i == 0:
        return dict(points[0][1])
    if i == len(points):
        return dict(points[-1][1])
    (x0, a), (x1, b) = points[i - 1], points[i]
    k = (hours - x0) / (x1 - x0) if x1 > x0 else 1.0
    return {key: a[key] + (b[key] - a[key]) * k for key in a if key in b}


def _name(seed: str, pid: str, sex: str) -> str:
    first = (_FIRST_F if sex == "F" else _FIRST_M)[int(_h(seed, "fn:" + pid) * 16) % 16]
    return f"{first} {_LAST[int(_h(seed, 'ln:' + pid) * 20) % 20]}"


def _member(p: PatientRecord, setting: str, anchor: datetime, seed: str) -> CohortMember:
    points = tuple(p.trajectory_points())
    start = dict(points[0][1]) if points else {}
    baseline = {**NORMAL, **{k: v for k, v in start.items() if k in VITALS}, **p.discharge_vitals}
    los = p.value("length_of_stay_days") or 0.0
    icu_los = p.value("icu_length_of_stay") or 1.0
    if setting == "icu":
        discharge_at, icu_admit_at = None, anchor - timedelta(days=min(icu_los, 14.0))
    else:
        offset_h = 2.0 + _h(seed, "offset:" + p.patient_id) * 22.0     # 2 to 24 hours into the post-discharge course
        discharge_at, icu_admit_at = anchor - timedelta(hours=offset_h), None
    condition = condition_of(p)
    return CohortMember(
        subject_id=p.subject_id, patient_id=p.patient_id, display_name=_name(seed, p.patient_id, p.sex), sex=p.sex,
        age=p.age, condition=condition, diagnosis=p.text("primary_diagnosis"), origin=p.origin, care_setting=setting,
        device_id=sim_device_id(p.patient_id, setting), discharge_at=discharge_at, icu_admit_at=icu_admit_at,
        hypercapnic=condition == "COPD", baseline=baseline, points=points, los_days=los,
        prior_admissions_12m=int(p.value("prior_admission_count_365d") or 0),
        drg_severity=int(v) if (v := p.value("drg_severity")) is not None else None,
        discharge_disposition=None if setting == "icu" else p.text("discharge_location"), record=p)


def build_cohort(data: TrainingData, anchor: datetime, icu: int = 6, home: int = 24, seed: str = "rpm-v1",
                 ) -> list[CohortMember]:
    """`icu` real patients shown as admitted, and `home` patients on wearables (0 = everyone else in the data).

    The choice is deterministic, so the seed tool, the simulator and the evaluation agree. Home patients are a third
    real MIMIC patients (they carry the richest record) and two thirds synthetic ones (they carry the full 72-hour
    wearable stream)."""
    def ranked(group: list[PatientRecord], tag: str) -> list[PatientRecord]:
        return sorted(group, key=lambda p: _h(seed, f"{tag}:{p.patient_id}"))

    real = ranked([p for p in data.patients.values() if p.real and p.discharge_vitals], "icu")
    admitted = real[:max(0, icu)]
    taken = {p.patient_id for p in admitted}
    rest_real = ranked([p for p in data.patients.values() if p.real and p.patient_id not in taken], "home")
    synthetic = ranked([p for p in data.patients.values() if not p.real], "home")
    usable = [p for p in rest_real if p.trajectory_points()], [p for p in synthetic if p.trajectory_points()]
    if home:
        n_real = min(len(usable[0]), home // 3)
        at_home = usable[0][:n_real] + usable[1][:home - n_real]
    else:
        at_home = usable[0] + usable[1]
    members = [_member(p, "icu", anchor, seed) for p in admitted] + [_member(p, "home", anchor, seed) for p in at_home]
    return _distinct_names(sorted(members, key=lambda m: m.patient_id), seed)


def _distinct_names(members: list[CohortMember], seed: str) -> list[CohortMember]:
    """Names are invented (the data has none), so two patients must never share one on the worklist."""
    seen: set[str] = set()
    out = []
    for m in members:
        name, i = m.display_name, 0
        while name in seen:
            i += 1
            given = name.split(" ")[0]
            name = f"{given} {_LAST[(int(_h(seed, 'ln:' + m.patient_id) * 20) + i) % 20]}" if i < 20 else \
                f"{m.display_name} {i}"
        seen.add(name)
        out.append(m if name == m.display_name else replace(m, display_name=name))
    return out


def profile_for_models(m: CohortMember) -> Mapping:
    """The `profile` part of a model plug-in's context, as the risk service builds it from the registry."""
    return {"patient_id": m.patient_id, "age": m.age, "sex": m.sex, "primary_condition": m.condition,
            "care_setting": m.care_setting, "los_days": m.los_days, "prior_admissions_12m": m.prior_admissions_12m,
            "drg_severity": m.drg_severity, "discharge_disposition": m.discharge_disposition,
            "discharge_at": m.discharge_at, "icu_admit_at": m.icu_admit_at,
            "hypercapnic_resp_failure": m.hypercapnic, "on_supplemental_o2": False}
