"""Serving matches training: the team's models get the same inputs from the platform as they were trained on.

Each test rebuilds a model's features through the platform's own path (the cohort reader, the patient context, the
plug-in's features.py) from the shared training data, and compares them with the teammate's own training table or
the training data's columns. Skipped where the git-ignored training data or hand-over folders are absent (CI).
"""

from __future__ import annotations

import csv
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from risk_service.plugins import PluginRegistry
from rpm_core.cohort import TrainingData

REPO = Path(__file__).resolve().parents[3]
DATA = REPO / "Training Data"
NAVYA = REPO / "Recovery Monitoring Score - Model Files" / "recovery_score_model" / "data" / "recovery_model_table.csv"
pytestmark = pytest.mark.skipif(not (DATA / "rpm_training_sample.csv").exists(),
                                reason="the team's training data is not available")
DISCHARGE = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def data():
    return TrainingData(DATA)


@pytest.fixture(scope="module")
def models():
    return PluginRegistry(REPO / "models" / "plugins").models


def ctx_at(p, hours: float, points) -> dict:
    """The patient context the risk service would build `hours` after discharge, with the recorded readings up to
    then as the vitals (the platform stores them as history with source 'record')."""
    now = DISCHARGE + timedelta(hours=hours)
    vitals = [{"ts": DISCHARGE + timedelta(hours=h), **v, "source": "record"} for h, v in points if h <= hours]
    return {"now": now, "care_setting": "home", "vitals": vitals, "daily": [], "labs": [],
            "record": p.record_facts, "last_recorded": p.last_recorded, "discharge_vitals": p.discharge_state,
            "profile": {"age": p.age, "sex": p.sex}, "hours_since_discharge": hours}


@pytest.mark.skipif(not NAVYA.exists(), reason="Navya's hand-over folder is not present")
def test_recovery_features_and_scores_match_navyas_training_table(data, models):
    """Synthetic patients: her table was built from the 72-hour stream, so the platform must rebuild it exactly."""
    plugin = models["recovery_monitoring"]
    rows = [r for r in csv.DictReader(NAVYA.open()) if r["data_origin"] == "synthetic"][:200]
    worst = 0.0
    for r in rows:
        p = data.patients[r["subject_id"]]
        built = plugin.build(ctx_at(p, float(r["hours_since_discharge"]), p.stream))
        for f in plugin.features:
            assert built[f] == pytest.approx(float(r[f]), abs=1e-6), (r["subject_id"], r["hours_since_discharge"], f)
        worst = max(worst, abs(plugin.predict(built).probability
                               - plugin.predict({f: float(r[f]) for f in plugin.features}).probability))
    assert worst < 1e-4


@pytest.mark.skipif(not NAVYA.exists(), reason="Navya's hand-over folder is not present")
def test_recovery_discharge_baseline_for_mimic_patients_is_what_navya_used(data):
    for r in csv.DictReader(NAVYA.open()):
        if r["data_origin"] != "mimic_real" or r["hours_since_discharge"] != "1.0":
            continue
        d = data.patients[f"RPM-{r['subject_id']}"].discharge_state
        navya = [float(r[f"discharge_{k}"]) for k in ("hr", "rr", "spo2", "sbp", "temp")]
        assert [d["heart_rate"], d["resp_rate"], d["spo2"], d["sbp"], d["temp"]] == pytest.approx(navya)


def test_readmission_scores_from_the_platform_match_tiyas_own_inputs(data, models):
    """At the 24-hour snapshot of every labelled admission: the platform's features, then the score, against the
    model scored on the training data's own columns (as Tiya's prepare_features builds them)."""
    plugin = models["readmission_risk"]
    rows = {r["patient_id"]: r for r in csv.DictReader((DATA / "rpm_training_sample.csv").open())
            if r["data_origin"] == "mimic_real" and float(r["hours_since_discharge"]) == 24.0}
    diffs, scored = [], []
    for pid, r in rows.items():
        p = data.patients[pid]
        built = plugin.build(ctx_at(p, 24.0, p.trajectory_points()))
        assert all(math.isfinite(v) for v in built.values())
        for f in ("age", "prior_admission_count_365d", "hospital_length_of_stay", "icu_length_of_stay",
                  "comorbidity_count", "high_risk_med_flag", "discharge_medication_count", "step_count",
                  "active_minutes", "activity_change_from_discharge", "wearable_adherence", "latest_heart_rate",
                  "heart_rate_change_from_discharge", "wearable_data_completness"):
            assert built[f] == pytest.approx(float(r[f]), abs=1e-6), (pid, f)
        own = {f: (1.0 if r["gender"].upper().startswith("M") else 0.0) if f == "gender_male" else
               (float(r[f]) if r.get(f) not in ("", None) else math.nan) for f in plugin.features}
        p_platform = plugin.predict(built).probability
        diffs.append(abs(p_platform - float(plugin._booster.predict([[own[f] for f in plugin.features]])[0])))
        scored.append((p_platform, float(r["readmit_30d_flag"] or 0) == 1.0))
    assert len(rows) == 63
    assert sorted(diffs)[len(diffs) // 2] < 0.01           # the typical patient scores the same
    pos = [s for s, y in scored if y]
    neg = [s for s, y in scored if not y]
    auroc = sum((a > b) + 0.5 * (a == b) for a in pos for b in neg) / (len(pos) * len(neg))
    assert auroc > 0.9                                       # her training data, so optimistic: a parity check only


def test_early_deterioration_reads_the_record_and_never_passes_a_gap(data, models):
    plugin = models["early_deterioration"]
    for pid in [p for p in data.patients if p.startswith("RPM-")][:20]:
        p = data.patients[pid]
        for hours in (1.0, 6.0, 24.0, 30.0):
            built = plugin.build(ctx_at(p, hours, p.trajectory_points()))
            assert all(math.isfinite(v) for v in built.values()), (pid, hours)
        for f in ("PTT", "pCO2", "bicarbonate", "potassium", "eGFR", "heart_rate", "non_invasive_bp_systolic"):
            if f in p.record_facts:
                assert built[f] == p.record_facts[f]


def test_no_label_reaches_a_model(data):
    labels = {"readmit_30d_flag", "readmit_90d_flag", "deterioration_next_3h", "deterioration_next_6h",
              "trajectory_type", "event_severity_fraction", "onset_time", "is_censored_last_admission",
              "recovery_score", "icu_to_discharge_days", "severity"}
    for p in data.patients.values():
        assert not labels & (set(p.record_facts) | set(p.last_recorded) | set(p.discharge_state))
