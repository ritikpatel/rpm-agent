import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rpm_core.cohort import TrainingData, build_cohort
from rpm_seed import history

DATA = Path(os.environ.get("RPM_DATA_DIR", Path(__file__).resolve().parents[3] / "Training Data"))
pytestmark = pytest.mark.skipif(not (DATA / "rpm_training_sample.csv").exists(),
                                reason="the team's training data is not available")
ANCHOR = datetime(2026, 10, 2, 12)


@pytest.fixture(scope="module")
def members():
    return build_cohort(TrainingData(DATA), ANCHOR)


def test_recorded_readings_become_history_that_ends_where_monitoring_starts(members):
    synthetic = next(m for m in members if m.origin == "synthetic")
    rows = history.rows(synthetic)
    assert len(rows) == 289                                                  # 72 hours, one reading every 15 minutes
    when = [r[2] for r in rows]
    assert when[0] == (ANCHOR - timedelta(hours=72)).replace(tzinfo=UTC) and when[-1] == ANCHOR.replace(tzinfo=UTC)
    last = dict(zip(history._COLUMNS, rows[-1], strict=True))
    assert last["source"] == "record" and last["context"] == "home" and last["device_id"] == synthetic.device_id
    assert last["heart_rate"] == synthetic.last["heart_rate"] and last["spo2"] == synthetic.last["spo2"]
    assert 0 <= last["news2"] <= 20 and last["news2_band"]
    assert len({r[3] for r in rows}) == len(rows)                            # one id per reading


def test_current_status_starts_as_the_last_recorded_reading(members):
    home = next(m for m in members if m.origin == "synthetic")
    device, last_seen, latest, news2, band, partial, detail, pid = history.status(home)
    assert (device, pid) == (home.device_id, home.patient_id) and last_seen == ANCHOR.replace(tzinfo=UTC)
    assert latest.obj["spo2"] == {"v": home.last["spo2"], "ts": last_seen.isoformat()}
    assert set(latest.obj) == {"heart_rate", "spo2", "resp_rate", "sbp", "temp"}
    assert news2 == history.rows(home)[-1][11] and band and detail.obj["total"] == news2
    icu = next(m for m in members if m.care_setting == "icu")
    _, seen, nothing, *scores, _ = history.status(icu)
    assert seen is None and nothing.obj == {} and scores == [None, None, None, None]   # until the monitor reports


def test_patients_shown_in_icu_have_no_home_history_and_labels_are_never_written(members):
    assert all(not history.rows(m) for m in members if m.care_setting == "icu")
    mimic = next(m for m in members if m.origin == "mimic_real" and m.care_setting == "home")
    assert 5 <= len(history.rows(mimic)) <= 6                                # discharge reading and the snapshots
    assert "severity" not in history._COLUMNS
    for m in members:
        for r in history.rows(m):
            assert len(r) == len(history._COLUMNS)
