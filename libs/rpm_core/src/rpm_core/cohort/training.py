"""Reader for the team's model-training data: the only source of patients for the platform.

Two files (kept out of git, in `Training Data/`):

- `rpm_training_sample.csv`: 263 patients, five snapshots each (1, 3, 6, 12 and 24 hours after discharge).
  63 are real MIMIC-IV-demo patients (`RPM-<subject_id>`, with the admission, diagnosis, labs and discharge
  baseline); 200 are synthetic (`SYN-<n>`, with demographics and vital signs).
- `synthetic_wearable_stream_72h.csv`: for the 200 synthetic patients, a wearable reading every 15 minutes for 72
  hours after discharge (heart rate, respiratory rate, SpO2, systolic blood pressure, temperature).

Standard library only, so every service and tool can read it. The ground-truth columns (`trajectory_type`,
`deterioration_*`, `event_severity_fraction`, `readmit_*`) are exposed for training and evaluation but are never put
in the hospital record, the guideline index or anything a model or agent sees at run time.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SAMPLE_FILE = "rpm_training_sample.csv"
STREAM_FILE = "synthetic_wearable_stream_72h.csv"
VITALS = ("heart_rate", "resp_rate", "spo2", "sbp", "temp")

# column -> (display name, unit, LOINC code)
LAB_COLUMNS: dict[str, tuple[str, str, str]] = {
    "creatinine": ("Creatinine", "mg/dL", "2160-0"), "BUN": ("Urea nitrogen", "mg/dL", "3094-0"),
    "glucose": ("Glucose", "mg/dL", "2345-7"), "sodium": ("Sodium", "mmol/L", "2951-2"),
    "potassium": ("Potassium", "mmol/L", "2823-3"), "chloride": ("Chloride", "mmol/L", "2075-0"),
    "bicarbonate": ("Bicarbonate", "mmol/L", "1963-8"), "anion_gap": ("Anion gap", "mmol/L", "33037-3"),
    "calcium": ("Calcium", "mg/dL", "17861-6"), "magnesium": ("Magnesium", "mg/dL", "19123-9"),
    "phosphate": ("Phosphate", "mg/dL", "2777-1"), "hemoglobin": ("Hemoglobin", "g/dL", "718-7"),
    "hematocrit": ("Hematocrit", "%", "4544-3"), "WBC": ("White blood cells", "10^3/uL", "6690-2"),
    "platelet_count": ("Platelets", "10^3/uL", "777-3"), "INR": ("INR", "", "6301-6"),
    "pH": ("pH", "", "2744-1"), "eGFR": ("eGFR", "mL/min/1.73m2", "33914-3"),
}

# What the team's models may read about a patient beyond the live readings, by name in the training data. An explicit
# allow-list: anything not named here (above all the label columns) never leaves the training files.
#
# Facts from the hospital record: the same on every snapshot of a patient (the admission, the ICU stay, the labs and
# vital signs charted before discharge).
RECORD_FACTS = (
    "age", "anchor_age", "prior_admission_count_90d", "prior_admission_count_365d", "hospital_length_of_stay",
    "icu_length_of_stay", "comorbidity_count", "chronic_condition_count", "high_risk_med_flag",
    "discharge_medication_count", "discharge_med_count", "BMI", "drg_severity", "drg_mortality",
    "heart_rate", "respiratory_rate", "O2_saturation_pulseoxymetry", "non_invasive_bp_systolic",
    "non_invasive_bp_diastolic", "temperature",
    "creatinine", "BUN", "bun_creatinine_ratio", "glucose", "sodium", "potassium", "chloride", "bicarbonate",
    "anion_gap", "calcium", "magnesium", "phosphate", "hemoglobin", "hematocrit", "WBC", "neutrophils",
    "lymphocytes", "neutrophil_lymphocyte_ratio", "platelet_count", "PT", "PTT", "INR", "pH", "pCO2", "pO2",
    "base_excess", "eGFR",
)
# Wearable values the platform does not stream (activity, steps, patient-reported scores) and the simulated watch's
# wear pattern: the values at the patient's last recorded snapshot, carried forward.
LAST_RECORDED = (
    "step_count", "active_minutes", "latest_activity_level", "activity_change_from_discharge", "activity_std_1h",
    "activity_std_3h", "activity_std_6h", "activity_std_12h", "activity_std_24h", "latest_pain_score",
    "latest_symptom_score", "symptom_score_change_from_discharge", "wearable_adherence", "wearable_data_completness",
)


def num(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if x == x else None  # NaN


def ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:19])
    except ValueError:
        return None


def _json(value) -> dict:
    try:
        doc = json.loads(value) if value else {}
    except ValueError:
        return {}
    return {k: v for k, v in doc.items() if v is not None} if isinstance(doc, dict) else {}


@dataclass
class PatientRecord:
    patient_id: str
    subject_id: str
    origin: str                                   # mimic_real | synthetic
    sex: str
    age: int
    trajectory: str                               # ground truth: stable | recovering | declining
    snapshots: list[dict] = field(default_factory=list)                    # training rows, by hours since discharge
    stream: list[tuple[float, dict[str, float]]] = field(default_factory=list)  # (hours since discharge, vitals)

    @property
    def row(self) -> dict:
        """The first snapshot: admission-level columns are the same on every row of a patient."""
        return self.snapshots[0]

    @property
    def real(self) -> bool:
        return self.origin == "mimic_real"

    def value(self, column: str) -> float | None:
        return num(self.row.get(column))

    def text(self, column: str) -> str | None:
        return self.row.get(column) or None

    def flag(self, column: str) -> bool:
        return (num(self.row.get(column)) or 0) > 0

    @property
    def discharge_vitals(self) -> dict[str, float]:
        """Vital signs charted at discharge (MIMIC patients), in the platform's names."""
        b = _json(self.row.get("discharge_vital_baseline"))
        gcs = [self.value(c) for c in ("gcs_eye", "gcs_motor", "gcs_verbal")]
        out = {"heart_rate": b.get("heart_rate"), "resp_rate": b.get("respiratory_rate"),
               "spo2": b.get("O2_saturation_pulseoxymetry"), "sbp": b.get("non_invasive_bp_systolic"),
               "dbp": b.get("non_invasive_bp_diastolic"), "temp": b.get("temperature"),
               "gcs": sum(gcs) if all(g is not None for g in gcs) else None}
        return {k: float(v) for k, v in out.items() if v is not None}

    @property
    def home_baseline(self) -> dict[str, float]:
        b = _json(self.row.get("simulated_home_baseline"))
        out = {"heart_rate": b.get("hr_rest"), "resp_rate": b.get("rr_rest"), "spo2": b.get("spo2_base"),
               "sbp": b.get("sbp_base"), "temp": b.get("temp_base"), "hrv_rmssd": b.get("hrv_base")}
        return {k: float(v) for k, v in out.items() if v is not None}

    @property
    def labs(self) -> dict[str, float]:
        return {c: v for c in LAB_COLUMNS if (v := self.value(c)) is not None}

    @property
    def record_facts(self) -> dict[str, float]:
        """Admission-level facts from the hospital record (RECORD_FACTS), for MIMIC patients; synthetic patients
        have no hospital record. Numbers only; `sex` is in the profile."""
        return {c: v for c in RECORD_FACTS if (v := self.value(c)) is not None} if self.real else {}

    @property
    def last_recorded(self) -> dict[str, float]:
        """LAST_RECORDED values at the last snapshot that has them (MIMIC patients)."""
        out: dict[str, float] = {}
        for s in self.snapshots:   # oldest first: later snapshots overwrite
            out.update({c: v for c in LAST_RECORDED if (v := num(s.get(c))) is not None})
        return out if self.real else {}

    @property
    def discharge_state(self) -> dict[str, float]:
        """Vital signs at discharge, as the team's models were trained on them: for MIMIC patients those charted in
        the ICU before discharge; for synthetic patients the first point of their wearable stream."""
        if self.stream:
            return {k: v for k, v in self.stream[0][1].items() if k in VITALS}
        names = {"heart_rate": "heart_rate", "resp_rate": "respiratory_rate", "spo2": "O2_saturation_pulseoxymetry",
                 "sbp": "non_invasive_bp_systolic", "temp": "temperature"}
        out = {k: v for k, col in names.items() if (v := self.value(col)) is not None}
        return out or {k: v for k, v in self.discharge_vitals.items() if k in VITALS}

    def trajectory_points(self) -> list[tuple[float, dict[str, float]]]:
        """Vital signs against hours since discharge. Synthetic patients: the 72-hour wearable stream. MIMIC
        patients: the discharge reading and the five snapshots, gaps filled from the patient's own baseline."""
        if self.stream:
            return self.stream
        fill = {**self.discharge_vitals, **self.home_baseline}
        points: list[tuple[float, dict[str, float]]] = []
        start = {k: self.discharge_vitals.get(k, fill.get(k)) for k in VITALS}
        if all(v is not None for v in start.values()):
            points.append((0.0, start))
        last = dict(fill)
        for s in self.snapshots:
            got = {"heart_rate": num(s.get("latest_heart_rate")), "resp_rate": num(s.get("latest_respiratory_rate")),
                   "spo2": num(s.get("latest_spo2")), "sbp": num(s.get("latest_sbp")),
                   "temp": num(s.get("latest_temp"))}
            last.update({k: v for k, v in got.items() if v is not None})
            if all(last.get(k) is not None for k in VITALS):
                points.append((float(s["hours_since_discharge"]), {k: float(last[k]) for k in VITALS}))
        return points


class TrainingData:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        if not (self.dir / SAMPLE_FILE).is_file():
            raise FileNotFoundError(f"{self.dir / SAMPLE_FILE} not found: put the team's training data there")
        self.patients: dict[str, PatientRecord] = {}
        with (self.dir / SAMPLE_FILE).open(newline="") as f:
            for r in csv.DictReader(f):
                pid = r["patient_id"]
                p = self.patients.get(pid)
                if p is None:
                    p = self.patients[pid] = PatientRecord(
                        patient_id=pid, subject_id=r["subject_id"], origin=r["data_origin"],
                        sex=r.get("gender") or "U", age=int(num(r.get("anchor_age")) or 0),
                        trajectory=r.get("trajectory_type") or "stable")
                r["hours_since_discharge"] = num(r.get("hours_since_discharge")) or 0.0
                p.snapshots.append(r)
        for p in self.patients.values():
            p.snapshots.sort(key=lambda r: r["hours_since_discharge"])
        stream = self.dir / STREAM_FILE
        if stream.is_file():
            with stream.open(newline="") as f:
                for r in csv.DictReader(f):
                    p = self.patients.get(r["patient_id"])
                    if p is None:
                        continue  # the stream never introduces a patient that is not in the sample
                    p.stream.append((float(r["hours_since_discharge"]), {
                        "heart_rate": float(r["heart_rate_bpm"]), "resp_rate": float(r["respiratory_rate_bpm"]),
                        "spo2": float(r["oxygen_saturation_pct"]), "sbp": float(r["systolic_bp_mmhg"]),
                        "temp": float(r["temperature_c"]), "severity": float(r["event_severity_fraction"] or 0)}))
            for p in self.patients.values():
                p.stream.sort(key=lambda x: x[0])

    def __len__(self) -> int:
        return len(self.patients)
