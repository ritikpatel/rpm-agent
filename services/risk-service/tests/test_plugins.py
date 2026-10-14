"""Contract check for every model plug-in under models/plugins (also run by `make model-check`)."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from risk_service.plugins import ContractError, ModelPlugin, PluginRegistry

ROOT = Path(__file__).resolve().parents[3] / "models" / "plugins"
NOW = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)
PLUGINS = sorted(p.parent for p in ROOT.glob("*/*/metadata.json"))
RECORD = {"age": 71.0, "anchor_age": 71.0, "prior_admission_count_365d": 1.0, "hospital_length_of_stay": 6.5,
          "icu_length_of_stay": 2.0, "comorbidity_count": 12.0, "high_risk_med_flag": 1.0,
          "discharge_medication_count": 3.0, "heart_rate": 88.0, "non_invasive_bp_systolic": 118.0, "PTT": 30.0,
          "pCO2": 40.0, "pO2": 90.0, "bicarbonate": 25.0, "potassium": 4.2, "eGFR": 80.0,
          "bun_creatinine_ratio": 18.0}
LAST = {"step_count": 900.0, "active_minutes": 3.0, "activity_change_from_discharge": -1.0, "activity_std_6h": 3.0,
        "wearable_adherence": 0.8, "wearable_data_completness": 0.9}
DISCHARGE = {"heart_rate": 84.0, "resp_rate": 18.0, "spo2": 96.0, "sbp": 124.0, "temp": 36.9}


def context(setting: str, rows: int = 120, deteriorating: bool = False, record: bool = True) -> dict:
    vitals = []
    for i in range(rows):
        k = i / max(1, rows - 1) if deteriorating else 0.0
        vitals.append({"ts": NOW - timedelta(minutes=rows - i), "heart_rate": 78 + 40 * k, "hrv_rmssd": 35.0,
                       "spo2": 97 - 9 * k, "resp_rate": (16 + 10 * k) if i % 10 == 0 else None,
                       "sbp": (122.0 - 30 * k) if i % 15 == 0 else None, "dbp": None,
                       "temp": (36.9 + 1.5 * k) if i % 10 == 0 else None, "skin_temp": 33.0, "gcs": None,
                       "steps": None, "activity": "resting", "news2": round(7 * k), "news2_partial": True,
                       "context": setting, "source": "simulator"})
    return {"now": NOW, "patient_id": "p-test", "care_setting": setting, "vitals": vitals, "daily": [], "labs": [],
            "record": dict(RECORD) if record else {}, "last_recorded": dict(LAST) if record else {},
            "discharge_vitals": dict(DISCHARGE),
            "profile": {"patient_id": "p-test", "age": 71, "sex": "F", "primary_condition": "CHF",
                        "care_setting": setting, "discharge_at": NOW - timedelta(days=3), "icu_admit_at": None,
                        "los_days": 5.0, "prior_admissions_12m": 1, "drg_severity": 3,
                        "discharge_disposition": "HOME", "hypercapnic_resp_failure": False,
                        "on_supplemental_o2": False},
            "hours_since_discharge": 72.0 if setting == "home" else None,
            "hours_in_icu": 30.0 if setting == "icu" else None}


@pytest.mark.parametrize("path", PLUGINS, ids=lambda p: f"{p.parent.name}-{p.name}")
def test_plugin_follows_the_contract(path: Path) -> None:
    plugin = ModelPlugin(path)
    assert plugin.name == path.parent.name and plugin.version == path.name
    for setting in plugin.meta["care_settings"]:
        for ctx in (context(setting), context(setting, deteriorating=True), context(setting, rows=0)):
            features = plugin.build(ctx)
            assert list(features) == plugin.features and all(math.isfinite(v) for v in features.values())
            pred = plugin.predict(features)
            assert 0.0 <= pred.probability <= 1.0 and pred.band in ("low", "medium", "high")
            out = plugin.explain(ctx, pred)
            assert out and out["headline"]["tone"] in ("good", "warn", "bad", "unknown") and out["badge"]
            json.dumps(out)
    json.dumps(plugin.describe())


def test_registry_serves_the_teams_three_models() -> None:
    registry = PluginRegistry(ROOT)
    assert not registry.errors
    assert {(m.name, m.meta["owner"]) for m in registry.models.values()} == {
        ("early_deterioration", "Harnoor"), ("readmission_risk", "Tiya"), ("recovery_monitoring", "Navya")}
    assert all(m.version == "1.0.0" and m._booster is not None for m in registry.models.values())


def test_models_trained_on_patients_with_a_hospital_record_need_one() -> None:
    registry = PluginRegistry(ROOT)
    with_record, without = context("home"), context("home", record=False)
    for name in ("readmission_risk", "early_deterioration"):
        m = registry.models[name]
        assert m.applies("on_demand", "home", with_record) and not m.applies("on_demand", "home", without)
        assert not m.applies("on_demand", "icu", with_record)              # trained after discharge only
    recovery = registry.models["recovery_monitoring"]
    assert recovery.applies("vitals", "home", without) and not recovery.applies("discharge", "home", without)


def test_recovery_scores_higher_when_vitals_worsen_and_explains_why() -> None:
    plugin = PluginRegistry(ROOT).models["recovery_monitoring"]
    calm = plugin.predict(plugin.build(context("home"))).probability
    worse = plugin.predict(plugin.build(context("home", deteriorating=True)))
    assert worse.probability > calm and worse.top_factors
    assert {"feature", "value", "contribution"} <= set(worse.top_factors[0])


def test_each_model_reads_its_result_the_way_its_owner_defined_it() -> None:
    models = PluginRegistry(ROOT).models
    recovery = models["recovery_monitoring"]
    for ctx, status in ((context("home"), "Good recovery"), (context("home", deteriorating=True), "Poor recovery")):
        out = recovery.explain(ctx, recovery.predict(recovery.build(ctx)))
        assert out["headline"]["label"] == "Recovery score" and out["headline"]["suffix"] == "/ 100"
        assert out["headline"]["status"] == status                # a high score is good, a low one is poor
    gone = context("home")
    gone["now"] = NOW + timedelta(hours=7)                         # nothing for 7 hours: no score, not an old one
    out = recovery.explain(gone, recovery.predict(recovery.build(gone)))
    assert out["headline"]["status"] == "Insufficient data" and out["headline"]["value"] is None
    readmit = models["readmission_risk"]
    out = readmit.explain(context("home"), readmit.predict(readmit.build(context("home"))))
    assert out["headline"]["label"] == "30-day readmission probability" and "Research prototype" in out["note"]
    deterioration = models["early_deterioration"]
    worse = context("home", deteriorating=True)
    out = deterioration.explain(worse, deterioration.predict(deterioration.build(worse)))
    assert out["headline"]["label"] == "Deterioration in the next 6 hours"
    assert any(c.startswith("SpO2 dropped") for c in out["concerns"])


def test_broken_plugin_is_rejected_with_a_clear_reason(tmp_path: Path) -> None:
    pkg = tmp_path / "bad" / "1.0.0"
    pkg.mkdir(parents=True)
    (pkg / "metadata.json").write_text(json.dumps({"name": "bad", "version": "1.0.0"}))
    with pytest.raises(ContractError, match="lacks"):
        ModelPlugin(pkg)
    assert "bad" in PluginRegistry(tmp_path).errors


def test_a_lightgbm_model_fitted_on_other_features_is_rejected(tmp_path: Path) -> None:
    src = ROOT / "readmission_risk" / "1.0.0"
    pkg = tmp_path / "readmission_risk" / "9.0.0"
    pkg.mkdir(parents=True)
    for f in ("metadata.json", "model.txt"):
        (pkg / f).write_bytes((src / f).read_bytes())
    (pkg / "features.py").write_text("FEATURES = ['age', 'hours_since_discharge']\ndef build(ctx):\n    return {}\n")
    with pytest.raises(ContractError, match="different features"):
        ModelPlugin(pkg)
