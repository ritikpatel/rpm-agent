"""Lazy, stdlib-only reader for MIMIC-IV-shaped CSV extracts (the synthetic cohort or the data team's real extract)."""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from functools import cached_property
from pathlib import Path

# chartevents itemids used for streaming vitals
HR, SPO2, RR, NBP_SYS, NBP_DIA, TEMP_C = "220045", "220277", "220210", "220179", "220180", "223762"
GCS_EYE, GCS_VERBAL, GCS_MOTOR = "220739", "223900", "223901"
VITAL_ITEMS = {HR: "heart_rate", SPO2: "spo2", RR: "resp_rate", NBP_SYS: "sbp", NBP_DIA: "dbp", TEMP_C: "temp"}
GCS_ITEMS = {GCS_EYE: (1, 4), GCS_VERBAL: (1, 5), GCS_MOTOR: (1, 6)}


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    if "." in value:  # MIMIC sometimes carries nanoseconds; datetime supports microseconds
        head, frac = value.split(".", 1)
        value = f"{head}.{frac[:6]}"
    return datetime.fromisoformat(value)


def to_float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


class MimicDataset:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        if not (self.root / "patients.csv").exists():
            raise FileNotFoundError(f"{self.root} does not look like a MIMIC extract (patients.csv missing)")

    def _read(self, name: str) -> list[dict[str, str]]:
        path = self.root / f"{name}.csv"
        if not path.exists():
            return []
        with path.open(newline="") as fh:
            return list(csv.DictReader(fh))

    @cached_property
    def patients(self) -> dict[str, dict[str, str]]:
        return {r["subject_id"]: r for r in self._read("patients")}

    @cached_property
    def admissions(self) -> list[dict[str, str]]:
        return sorted(self._read("admissions"), key=lambda r: (r["subject_id"], r["admittime"]))

    @cached_property
    def admissions_by_subject(self) -> dict[str, list[dict[str, str]]]:
        out: dict[str, list[dict[str, str]]] = defaultdict(list)
        for r in self.admissions:
            out[r["subject_id"]].append(r)
        return out

    @cached_property
    def icustays_by_hadm(self) -> dict[str, list[dict[str, str]]]:
        out: dict[str, list[dict[str, str]]] = defaultdict(list)
        for r in self._read("icustays"):
            out[r["hadm_id"]].append(r)
        for v in out.values():
            v.sort(key=lambda r: r["intime"])
        return out

    @cached_property
    def chartevents_by_stay(self) -> dict[str, list[dict[str, str]]]:
        out: dict[str, list[dict[str, str]]] = defaultdict(list)
        for r in self._read("chartevents"):
            out[r["stay_id"]].append(r)
        return out

    def table(self, name: str) -> list[dict[str, str]]:
        return self._read(name)

    def dictionary(self, name: str, key: str) -> dict[str, dict[str, str]]:
        return {r[key]: r for r in self._read(name)}
