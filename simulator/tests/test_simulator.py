import json
import os
import time
from datetime import datetime
from pathlib import Path

import pytest

from rpm_core.contracts import Command, DeviceEvent, Telemetry
from rpm_core.mimic import MimicDataset, build_cohort, icu_series
from rpm_simulator.engine import Simulator
from rpm_simulator.scenarios import SCENARIOS, ActiveScenario

DATA = Path(os.environ.get("MIMIC_DIR", Path(__file__).resolve().parents[2] / "synthetic_mimic_150patients"))
pytestmark = pytest.mark.skipif(not (DATA / "patients.csv").exists(), reason="MIMIC extract not available")


@pytest.fixture(scope="module")
def cohort_and_series():
    ds = MimicDataset(DATA)
    members = build_cohort(ds, datetime(2026, 10, 2, 12), icu_count=25)
    series = {m.subject_id: icu_series(ds, m.index_stay_id) for m in members if m.care_setting == "icu"}
    return members, series


def make_sim(cohort_and_series, **kw):
    members, series = cohort_and_series
    sent: list[tuple[str, str]] = []
    sim = Simulator(members, series, lambda k, t, p: sent.append((t, p)), **kw)
    for d in sim.devices.values():
        d.next_due = 0  # everything due now
    return sim, sent


def test_every_device_publishes_valid_telemetry(cohort_and_series):
    sim, sent = make_sim(cohort_and_series)
    sim.tick()
    assert len(sent) == 150
    icu = 0
    for topic, payload in sent:
        t = Telemetry.model_validate_json(payload)
        assert topic == f"rpm/v1/dev/{t.device_id}/telemetry"
        icu += t.context == "icu"
        if t.context == "icu":  # full NEWS2 parameter set from the bedside
            assert {"heart_rate", "spo2", "resp_rate", "sbp", "temp"} <= set(t.metrics.model_dump(exclude_none=True))
    assert icu >= 22


def test_scenario_ramps_heart_rate(cohort_and_series):
    sim, sent = make_sim(cohort_and_series)
    dev = next(d for d in sim.devices.values() if d.context == "home")
    before = []
    for _ in range(5):
        before.append(sim.devices[dev.device_id].gen.sample(0, 12, 30, {}, False, False).metrics["heart_rate"]["mean"])
    sc = ActiveScenario(SCENARIOS["af_rvr"], time.monotonic() - 100, 600)
    dev.scenario = sc
    deltas, *_ = sim._deltas(dev, time.monotonic())
    assert deltas["heart_rate"] == pytest.approx(62)


def test_offline_buffers_then_backfills(cohort_and_series):
    sim, sent = make_sim(cohort_and_series)
    dev = next(d for d in sim.devices.values() if d.context == "home")
    dev.scenario = ActiveScenario(SCENARIOS["device_offline"], time.monotonic(), 1000)
    for _ in range(3):
        sim._produce(dev, time.monotonic())
    assert len(dev.outbox) == 3 and not [s for s in sent if dev.device_id in s[0]]
    dev.scenario = None
    sim._produce(dev, time.monotonic())
    mine = [Telemetry.model_validate_json(p) for t, p in sent if dev.device_id in t]
    assert len(mine) == 4 and [m.edge.buffered for m in mine] == [True, True, True, False]
    assert [m.seq for m in mine] == sorted(m.seq for m in mine)


def test_checkin_command_gets_reply(cohort_and_series):
    sim, sent = make_sim(cohort_and_series)
    dev = next(d for d in sim.devices.values() if d.context == "home")
    cmd = Command(cmd="CHECKIN", args={"question_id": "q1", "text": "Breathless?", "options": ["Yes", "No"]})
    sim.on_message(f"rpm/v1/dev/{dev.device_id}/cmd", cmd.to_json().encode())
    dev.pending = [(0, *p[1:]) for p in dev.pending]
    sim._pending(dev, time.monotonic())
    ev = DeviceEvent.model_validate_json(sent[-1][1])
    assert ev.type == "CHECKIN_REPLY" and ev.data["answer"] == "No"


def test_set_profile_shortens_window(cohort_and_series):
    sim, _ = make_sim(cohort_and_series)
    dev = next(iter(sim.devices.values()))
    sim.on_message(f"rpm/v1/dev/{dev.device_id}/cmd",
                   Command(cmd="SET_PROFILE", args={"profile": "critical"}).to_json().encode())
    assert dev.window_s == 5 and dev.profile == "critical"


def test_control_topic_starts_scenario(cohort_and_series):
    sim, _ = make_sim(cohort_and_series)
    sim.on_message("rpm/v1/sim/control",
                   json.dumps({"action": "scenario", "target": "p-90000001", "scenario": "sepsis"}).encode())
    assert sim.resolve("p-90000001").scenario.spec.name == "sepsis"


def test_sos_is_emitted_immediately_even_if_next_tick_is_late(cohort_and_series):
    sim, sent = make_sim(cohort_and_series)
    dev = next(d for d in sim.devices.values() if d.context == "home")
    dev.next_due = time.monotonic() + 3600
    assert sim.start_scenario(dev.device_id, "sos")
    events = [DeviceEvent.model_validate_json(p) for t, p in sent if t.endswith("/events")]
    assert [e.type for e in events] == ["SOS"]


def test_bradycardia_targets_an_absolute_rate_whatever_the_baseline(cohort_and_series):
    members, series = cohort_and_series
    home = [m for m in members if m.care_setting == "home"]
    fast, slow = max(home, key=lambda m: m.baseline["heart_rate"]), min(home, key=lambda m: m.baseline["heart_rate"])
    sim = Simulator([fast, slow], {}, lambda *_: None)
    for m in (fast, slow):
        sim.start_scenario(m.patient_id, "bradycardia")
        dev = sim.devices[m.device_id]
        dev.scenario.started -= 120  # past the 60 s ramp: full effect
        deltas, *_ = sim._deltas(dev, time.monotonic())
        assert m.baseline["heart_rate"] + deltas["heart_rate"] == pytest.approx(38)


def _by_patient(sim, sent, patient_id):
    sent.clear()
    for d in sim.devices.values():
        d.next_due = 0
    sim.tick()
    out = [Telemetry.model_validate_json(p) for _, p in sent]
    return [t for t in out if t.device_id.endswith(patient_id.removeprefix("p-"))]


def test_icu_monitor_only_while_admitted_and_wearable_only_at_home(cohort_and_series):
    sim, sent = make_sim(cohort_and_series)
    icu = next(m for m in cohort_and_series[0] if m.care_setting == "icu")
    before = _by_patient(sim, sent, icu.patient_id)
    assert [(t.source, t.context, t.device_id) for t in before] == [("icu_monitor", "icu", icu.device_id)]

    sim.on_message("rpm/v1/sim/control", json.dumps({"action": "discharge", "target": icu.patient_id}).encode())
    after = _by_patient(sim, sent, icu.patient_id)
    assert [(t.source, t.context) for t in after] == [("simulator", "home")]   # the ICU monitor is gone
    assert after[0].device_id == f"sim-{icu.subject_id}" and after[0].metrics.activity is not None

    sim.on_message("rpm/v1/sim/control", json.dumps({"action": "admit", "target": icu.patient_id}).encode())
    again = _by_patient(sim, sent, icu.patient_id)
    assert [(t.source, t.device_id) for t in again] == [("icu_monitor", icu.device_id)]


def test_registry_sync_follows_settings_and_pauses_for_a_real_watch(cohort_and_series):
    sim, sent = make_sim(cohort_and_series)
    home = next(m for m in cohort_and_series[0] if m.care_setting == "home")
    icu = next(m for m in cohort_and_series[0] if m.care_setting == "icu")
    sim.sync([{"patient_id": home.patient_id, "care_setting": "home", "device_kind": "watch"},
              {"patient_id": icu.patient_id, "care_setting": "home", "device_kind": "simulator"},
              {"patient_id": "p-unknown", "care_setting": "icu", "device_kind": None}])
    assert _by_patient(sim, sent, home.patient_id) == []            # a real watch speaks for this patient now
    assert _by_patient(sim, sent, icu.patient_id)[0].context == "home"
    sim.sync([{"patient_id": home.patient_id, "care_setting": "home", "device_kind": "simulator"}])
    assert len(_by_patient(sim, sent, home.patient_id)) == 1
