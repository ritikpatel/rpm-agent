"""Contract check for every model plug-in under models/plugins (also run by `make model-check`)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from risk_service.plugins import ContractError, ModelPlugin, PluginRegistry

ROOT = Path(__file__).resolve().parents[3] / "models" / "plugins"
NOW = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)
FITTED = sorted(p.parent for p in ROOT.glob("*/*/model.*") if p.suffix in (".joblib", ".onnx"))

pytestmark = pytest.mark.skipif(not FITTED, reason="no fitted models (run `make models`)")


def context(setting: str, rows: int = 120, deteriorating: bool = False) -> dict:
    vitals = []
    for i in range(rows):
        k = i / max(1, rows - 1) if deteriorating else 0.0
        vitals.append({"ts": NOW - timedelta(minutes=rows - i), "heart_rate": 78 + 40 * k, "hrv_rmssd": 35.0,
                       "spo2": 97 - 9 * k, "resp_rate": (16 + 10 * k) if setting == "icu" else None,
                       "sbp": 122.0 if setting == "icu" else None, "dbp": 76.0 if setting == "icu" else None,
                       "temp": 36.9 if setting == "icu" else None, "skin_temp": 33.0, "gcs": 15.0, "steps": 12.0,
                       "activity": "resting", "news2": round(7 * k), "news2_partial": setting == "home",
                       "context": setting})
    daily = [{"day": NOW - timedelta(days=d), "windows": 2000, "hr_mean": 76.0, "hr_min": 58.0, "hr_max": 110.0,
              "spo2_mean": 96.5, "spo2_min": 93.0, "rr_max": 20.0, "temp_max": 37.1, "hrv_mean": 34.0,
              "steps": 3000.0, "news2_max": 2, "news2_mean": 0.6} for d in range(6, -1, -1)]
    return {"now": NOW, "patient_id": "p-test", "care_setting": setting, "vitals": vitals,
            "daily": daily if rows else [], "labs": [],
            "profile": {"patient_id": "p-test", "age": 71, "sex": "F", "primary_condition": "CHF",
                        "care_setting": setting, "discharge_at": NOW - timedelta(days=3), "icu_admit_at": None,
                        "los_days": 5.0, "prior_admissions_12m": 1, "drg_severity": 3,
                        "discharge_disposition": "HOME", "hypercapnic_resp_failure": False,
                        "on_supplemental_o2": False},
            "hours_since_discharge": 72.0 if setting == "home" else None,
            "hours_in_icu": 30.0 if setting == "icu" else None}


@pytest.mark.parametrize("path", FITTED, ids=lambda p: f"{p.parent.name}-{p.name}")
def test_plugin_follows_the_contract(path: Path) -> None:
    plugin = ModelPlugin(path)
    assert plugin.name == path.parent.name and plugin.version == path.name
    for setting in plugin.meta["care_settings"]:
        for ctx in (context(setting), context(setting, deteriorating=True), context(setting, rows=0)):
            features = plugin.build(ctx)
            assert list(features) == plugin.features
            pred = plugin.predict(features)
            assert 0.0 <= pred.probability <= 1.0 and pred.band in ("low", "medium", "high")
    json.dumps(plugin.describe())


def test_deterioration_scores_higher_when_vitals_worsen() -> None:
    registry = PluginRegistry(ROOT)
    plugin = registry.models["early_deterioration"]
    calm = plugin.predict(plugin.build(context("icu"))).probability
    worse = plugin.predict(plugin.build(context("icu", deteriorating=True)))
    assert worse.probability > calm and worse.top_factors


def test_registry_serves_the_three_models_and_respects_triggers() -> None:
    registry = PluginRegistry(ROOT)
    assert {"early_deterioration", "readmission_risk", "recovery_monitoring"} <= set(registry.models)
    assert not registry.errors
    readmit = registry.models["readmission_risk"]
    assert readmit.applies("discharge", "home") and not readmit.applies("vitals", "home")
    assert not readmit.applies("discharge", "icu") and readmit.applies("on_demand", "home")


def test_broken_plugin_is_rejected_with_a_clear_reason(tmp_path: Path) -> None:
    pkg = tmp_path / "bad" / "1.0.0"
    pkg.mkdir(parents=True)
    (pkg / "metadata.json").write_text(json.dumps({"name": "bad", "version": "1.0.0"}))
    with pytest.raises(ContractError, match="lacks"):
        ModelPlugin(pkg)
    assert "bad" in PluginRegistry(tmp_path).errors
