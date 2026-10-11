"""The cohort comes from the team's training data and from nowhere else."""

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from rpm_core.cohort import TrainingData, build_cohort, trajectory_at

DATA = Path(os.environ.get("RPM_DATA_DIR", Path(__file__).resolve().parents[3] / "Training Data"))
pytestmark = pytest.mark.skipif(not (DATA / "rpm_training_sample.csv").exists(),
                                reason="the team's training data is not available")
ANCHOR = datetime(2026, 10, 2, 12)


@pytest.fixture(scope="module")
def data():
    return TrainingData(DATA)


def test_every_member_is_a_patient_in_the_training_data(data):
    everyone = build_cohort(data, ANCHOR, home=0)
    assert {m.patient_id for m in everyone} == set(data.patients)       # nobody added, nobody invented
    assert len(everyone) == len(data) == 263
    default = build_cohort(data, ANCHOR)
    assert len(default) == 30 and {m.patient_id for m in default} <= set(data.patients)
    assert [m.patient_id for m in build_cohort(data, ANCHOR)] == [m.patient_id for m in default]   # deterministic


def test_only_real_patients_are_shown_in_icu_and_everyone_else_is_home(data):
    members = build_cohort(data, ANCHOR)
    icu = [m for m in members if m.care_setting == "icu"]
    assert len(icu) == 6 and all(m.origin == "mimic_real" and m.discharge_at is None and m.icu_admit_at for m in icu)
    assert all(m.device_id == f"sim-icu-{m.patient_id}" for m in icu)
    home = [m for m in members if m.care_setting == "home"]
    assert all(m.discharge_at < ANCHOR and m.device_id == f"sim-{m.patient_id}" for m in home)
    assert {m.origin for m in home} == {"mimic_real", "synthetic"}


def test_monitoring_starts_where_each_patients_recorded_data_ends(data):
    members = build_cohort(data, ANCHOR)
    for m in members:
        if m.care_setting == "home":
            end_h, reading = m.points[-1]
            assert end_h == (72.0 if m.origin == "synthetic" else 24.0)
            assert m.discharge_at == ANCHOR - timedelta(hours=end_h)          # the last recorded reading is "now"
            assert m.last == {k: reading[k] for k in ("heart_rate", "resp_rate", "spo2", "sbp", "temp")}
        else:   # shown as admitted: the last ICU readings the data has are the ones charted at discharge
            assert m.last == {k: m.baseline[k] for k in m.last} and set(m.last) <= set(m.baseline)
        assert "severity" not in m.last                                        # a label, never a reading


def test_every_patient_has_their_own_settled_values_from_the_data(data):
    for m in build_cohort(data, ANCHOR, home=0):
        assert set(m.well) == set(m.last) and "severity" not in m.well
        if m.origin == "mimic_real":       # the home baseline the data gives
            assert m.well["heart_rate"] == pytest.approx(m.record.home_baseline["heart_rate"])
        elif m.care_setting == "home":     # the median of their own readings outside deterioration episodes
            assert 93 <= m.well["spo2"] <= 100 and 55 <= m.well["heart_rate"] <= 100
        assert m.live_from_h == (m.points[-1][0] if m.care_setting == "home" else 0.0)


def test_only_the_offline_evaluation_places_patients_inside_their_recorded_data(data):
    for m in build_cohort(data, ANCHOR, replay=True):
        if m.care_setting == "home":
            assert timedelta(hours=2) <= ANCHOR - m.discharge_at <= timedelta(hours=24)


def test_recorded_readings_are_read_exactly_and_interpolated_only_for_the_replay(data):
    m = next(x for x in build_cohort(data, ANCHOR) if x.origin == "synthetic")
    stream = data.patients[m.patient_id].stream
    assert len(stream) == 289 and stream[0][0] == 0.0 and stream[-1][0] == 72.0
    h, exact = stream[40]
    assert trajectory_at(m.points, h)["heart_rate"] == pytest.approx(exact["heart_rate"])
    between = trajectory_at(m.points, h + 0.125)["spo2"]
    assert min(exact["spo2"], stream[41][1]["spo2"]) <= between <= max(exact["spo2"], stream[41][1]["spo2"])


def test_real_patients_carry_their_admission_and_labs(data):
    m = next(x for x in build_cohort(data, ANCHOR) if x.origin == "mimic_real" and x.care_setting == "home")
    assert m.diagnosis and m.los_days > 0 and m.record.labs and len(m.points) >= 5
    assert {"heart_rate", "spo2", "resp_rate", "sbp", "temp"} <= set(m.baseline)
