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
def test_recovery_output_reproduces_navyas_own_example(models):
    """Her sample request (patient P10002428, 6 hours after discharge) through the platform, against the output of
    her RecoveryModel.score() as her DEPLOYMENT.md documents it."""
    import json

    base = NAVYA.parents[1] / "deployment"
    req = json.loads((base / "sample_request.json").read_text())
    col = {"heart_rate": "heart_rate", "respiratory_rate": "resp_rate", "spo2": "spo2", "sbp": "sbp",
           "temperature": "temp"}

    def t(s):
        return datetime.fromisoformat(s).replace(tzinfo=UTC)
    vitals = sorted(({"ts": t(r["timestamp"]), col[r["channel"]]: r["value"], "source": "watch"}
                     for r in req["readings"] if r["channel"] in col), key=lambda r: r["ts"])
    dv = req["discharge_vitals"]
    ctx = {"now": t(req["scoring_time"]), "vitals": vitals, "care_setting": "home", "record": {}, "last_recorded": {},
           "discharge_vitals": {"heart_rate": dv["hr"], "resp_rate": dv["rr"], "spo2": dv["spo2"], "sbp": dv["sbp"],
                                "temp": dv["temp"]}, "profile": {"age": req["age"], "sex": req["sex"]},
           "hours_since_discharge": (t(req["scoring_time"]) - t(req["discharge_time"])).total_seconds() / 3600}
    plugin = models["recovery_monitoring"]
    out = plugin.explain(ctx, plugin.predict(plugin.build(ctx)))
    for k, v in {"recovery_score": 57.8, "recovery_status": "Moderate recovery", "trend_direction": "Worsening",
                 "score_change_1h": -7.8, "score_change_3h": -9.5, "score_change_24h": -37.1,
                 "sudden_decline_flag": False, "poor_recovery_probability": 0.996, "poor_recovery_alert": True,
                 "data_freshness": "Recent", "vitals_imputed_from_discharge": 2}.items():
        assert out["values"][k] == v, k
    assert out["concerns"] == ["Current oxygen saturation (91.0 %)", "Recovery score (57.8 pts)",
                               "Current heart rate (118.0 bpm)"]
    assert out["reassurances"] == ["Highest heart rate (6h) (128.0 bpm)", "Respiratory rate at discharge (19.0 /min)"]


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


def test_early_deterioration_1_1_is_the_retrained_model_with_live_inputs_only(models):
    """1.1.0 reads what the platform can supply live, and its threshold and evidence are the training script's."""
    import importlib.util

    plugin = models["early_deterioration"]
    assert plugin.version == "1.1.0" and plugin.meta["validated"]           # validated on the shared data's CV
    spec = importlib.util.spec_from_file_location("ed_train", REPO / "models/training/early_deterioration/train.py")
    train = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(train)
    fb = train.builder()
    assert plugin.features == [f for f in fb.FEATURES if f not in fb.CARRIED and f not in train.TIME]
    assert "hours_since_discharge" not in plugin.features
    assert plugin.meta["bands"]["high"] == plugin.meta["metrics"]["alert_threshold"]
    assert plugin.meta["metrics"]["cv_auroc_mean"] >= 0.75 and plugin.meta["metrics"]["cv_auroc_mimic_rows"] >= 0.75


def test_retrained_early_deterioration_is_not_a_clock(data, models):
    """Without the hours since discharge, the same physiology scores the same at 6 and at 72 hours."""
    plugin = models["early_deterioration"]
    p = next(p for p in data.patients.values() if not p.real and p.stream)
    early, late = ctx_at(p, 6.0, p.stream), ctx_at(p, 6.0, p.stream)
    late["hours_since_discharge"] = 72.0
    assert plugin.predict(plugin.build(early)).probability == plugin.predict(plugin.build(late)).probability
