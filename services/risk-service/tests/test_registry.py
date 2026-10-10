from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from risk_service.registry import Registry
from rpm_core.features.features import instability_features

ROOT = Path(__file__).resolve().parents[3] / "models" / "registry"
pytestmark = pytest.mark.skipif(not ROOT.exists(), reason="run `make models` to build baseline packages")


@pytest.fixture(scope="module")
def reg():
    return Registry(ROOT)


def test_all_baselines_load_and_are_flagged_unvalidated(reg):
    assert {"readmission_30d", "icu_mortality", "instability_6h"} <= set(reg.models)
    assert reg.get("instability_6h").meta["validated"] is False


def test_instability_rises_with_deterioration(reg):
    now = datetime.now(UTC)
    calm = [{"ts": now - timedelta(minutes=50 - i * 10), "heart_rate": 78, "spo2": 97, "resp_rate": 16, "news2": 0,
             "news2_partial": False} for i in range(6)]
    worse = [{"ts": now - timedelta(minutes=50 - i * 10), "heart_rate": 90 + i * 8, "spo2": 95 - i,
              "resp_rate": 18 + i, "news2": i + 2, "news2_partial": False} for i in range(6)]
    pkg = reg.get("instability_6h")
    p_calm = pkg.predict(instability_features(calm)[0])
    p_worse = pkg.predict(instability_features(worse)[0])
    assert p_calm.probability < 0.05 < 0.5 < p_worse.probability
    assert p_worse.band == "high" and p_worse.top_factors[0]["feature"] in {"news2_now", "news2_delta_1h"}


def test_missing_feature_rejected(reg):
    with pytest.raises(ValueError):
        reg.get("readmission_30d").predict({"age": 70})
