import os
from datetime import datetime
from pathlib import Path

import pytest

from rpm_mimic import MimicDataset, build_cohort

DATA = Path(os.environ.get("MIMIC_DIR", Path(__file__).resolve().parents[3] / "synthetic_mimic_150patients"))
pytestmark = pytest.mark.skipif(not (DATA / "patients.csv").exists(), reason="MIMIC extract not available")
ANCHOR = datetime(2026, 10, 2, 12, 0, tzinfo=None)


@pytest.fixture(scope="module")
def cohort():
    return build_cohort(MimicDataset(DATA), ANCHOR, icu_count=25)


def test_cohort_is_deterministic(cohort):
    again = build_cohort(MimicDataset(DATA), ANCHOR, icu_count=25)
    assert [(m.patient_id, m.care_setting, m.device_id, m.time_shift) for m in cohort] == \
           [(m.patient_id, m.care_setting, m.device_id, m.time_shift) for m in again]


def test_every_patient_once_and_icu_share(cohort):
    assert len({m.patient_id for m in cohort}) == len(cohort) == 150
    icu = [m for m in cohort if m.care_setting == "icu"]
    assert 22 <= len(icu) <= 25


def test_home_patients_discharged_recently(cohort):
    for m in cohort:
        if m.care_setting == "home":
            days = (ANCHOR - m.discharge_at).days
            assert 0 <= days <= 10, m


def test_baselines_are_physiological(cohort):
    for m in cohort:
        b = m.baseline
        assert 55 <= b["heart_rate"] <= 100 and 88 <= b["spo2"] <= 100 and 35.5 <= b["temp"] <= 38
