import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from rpm_core.cohort import TrainingData, build_cohort, trajectory_at
from rpm_core.contracts import Command, DeviceEvent, Telemetry
from rpm_simulator.engine import Simulator
from rpm_simulator.physiology import ContinuingWearable, TrajectoryWearable
from rpm_simulator.scenarios import SCENARIOS, ActiveScenario

DATA = Path(os.environ.get("RPM_DATA_DIR", Path(__file__).resolve().parents[2] / "Training Data"))
pytestmark = pytest.mark.skipif(not (DATA / "rpm_training_sample.csv").exists(),
                                reason="the team's training data is not available")
ANCHOR = datetime(2026, 10, 2, 12)


@pytest.fixture(scope="module")
def cohort_and_series():
    return build_cohort(TrainingData(DATA), ANCHOR), None


def make_sim(cohort_and_series, **kw):
    members, _ = cohort_and_series
    sent: list[tuple[str, str]] = []
    sim = Simulator(members, lambda k, t, p: sent.append((t, p)), **kw)
    for d in sim.devices.values():
        d.next_due = 0  # everything due now
    return sim, sent


def test_every_device_publishes_valid_telemetry(cohort_and_series):
    sim, sent = make_sim(cohort_and_series)
    sim.tick()
    members = cohort_and_series[0]
    assert len(sent) == len(members) == 30
    icu = 0
    for topic, payload in sent:
        t = Telemetry.model_validate_json(payload)
        assert topic == f"rpm/v1/dev/{t.device_id}/telemetry"
        icu += t.context == "icu"
        if t.context == "icu":  # full NEWS2 parameter set from the bedside
            assert t.source == "icu_monitor"
            assert {"heart_rate", "spo2", "resp_rate", "sbp", "temp"} <= set(t.metrics.model_dump(exclude_none=True))
    assert icu == 6


def test_wearable_carries_on_from_the_last_recorded_reading(cohort_and_series):
    """Live readings are new: they start on the patient's last recorded values. With recovery switched off they
    stay around them for good."""
    members, _ = cohort_and_series
    home = [m for m in members if m.care_setting == "home"]
    sim = Simulator(home, lambda *_: None, recovery_half_life_h=0)
    for m in home:
        gen = sim.devices[m.device_id].gen
        assert isinstance(gen, ContinuingWearable) and gen.last == m.last
        assert gen.values(gen.t0) == pytest.approx(m.last)                    # the first value is the last recorded
        seen = {k: [] for k in m.last}
        for i in range(1, 2881):                                              # a day of 30-second windows
            w = gen.sample(gen.t0 + i * 30, 12, 30, {}, False, False, force_spo2=True).metrics
            seen["heart_rate"].append(w["heart_rate"]["mean"])
            for k in ("spo2", "resp_rate", "temp"):
                seen[k].append(w[k]["value"])
        mean = {k: sum(v) / len(v) for k, v in seen.items() if v}
        assert mean["heart_rate"] == pytest.approx(m.last["heart_rate"], abs=1.5)
        assert mean["spo2"] == pytest.approx(min(m.last["spo2"], 99.7), abs=0.5)
        assert mean["resp_rate"] == pytest.approx(m.last["resp_rate"], abs=0.8)
        assert max(seen["heart_rate"]) - min(seen["heart_rate"]) < 25         # wanders, does not run away
    readings = [sim.devices[m.device_id].gen.sample(1e9, 12, 30, {}, False, False).metrics["heart_rate"]["mean"]
                for m in home[:2]]
    assert readings[0] != readings[1]                                         # every patient has their own stream


def _unwell(members):
    return [m for m in members if m.care_setting == "home" and m.last["spo2"] < 92]


def test_unwell_patients_recover_gradually_towards_their_own_settled_values(cohort_and_series):
    members, _ = cohort_and_series
    unwell = _unwell(members)
    assert len(unwell) == 8                                   # of the 24 at home in the default cohort
    sim = Simulator(unwell, lambda *_: None)                  # default: the gap to well halves every two hours
    for m in unwell:
        gen = sim.devices[m.device_id].gen
        assert m.well["spo2"] > 95 and m.well["heart_rate"] < m.last["heart_rate"]
        assert gen.centre(gen.t0) == pytest.approx(m.last) and not gen.settled(gen.t0)   # starts as recorded
        for hours, left in ((2, 0.5), (4, 0.25), (24, 0.0)):
            now = gen.centre(gen.t0 + hours * 3600)
            for k in ("spo2", "heart_rate", "resp_rate"):
                assert now[k] == pytest.approx(m.well[k] + (m.last[k] - m.well[k]) * left, abs=0.05)
        assert gen.centre(gen.t0 + 1800)["spo2"] < 92         # still unwell after half an hour: no sudden cure
        assert gen.settled(gen.t0 + 24 * 3600)
    stable = next(m for m in members if m.care_setting == "home" and m.origin == "synthetic" and m not in unwell)
    gen = Simulator([stable], lambda *_: None).devices[stable.device_id].gen
    assert gen.settled(gen.t0) and gen.centre(gen.t0 + 6 * 3600)["spo2"] == pytest.approx(stable.well["spo2"], abs=1)


def test_a_restarted_simulator_carries_on_from_what_the_device_last_reported(cohort_and_series):
    members, _ = cohort_and_series
    m = _unwell(members)[0]
    row = {"patient_id": m.patient_id, "care_setting": "home", "device_kind": "simulator",
           "discharge_at": m.discharge_at.isoformat() + "+00:00"}
    live_from = m.discharge_at + timedelta(hours=m.live_from_h)

    def restarted(latest):
        sim = Simulator([m], lambda *_: None)
        sim.sync([{**row, "latest": latest}])
        gen = sim.devices[m.device_id].gen
        return gen.centre(time.monotonic())["spo2"]

    partly = {"spo2": {"v": 94.0, "ts": (live_from + timedelta(hours=3)).isoformat() + "+00:00"}}
    assert restarted(partly) == pytest.approx(94.0, abs=0.1)                     # not back to the recorded 89
    recorded = {"spo2": {"v": m.last["spo2"], "ts": live_from.isoformat() + "+00:00"}}
    assert restarted(recorded) == pytest.approx(m.last["spo2"], abs=0.1)         # nothing live yet: the record
    assert restarted(None) == pytest.approx(m.last["spo2"], abs=0.1)


def test_settled_patients_deteriorate_now_and_then_and_recovering_ones_are_left_alone(cohort_and_series):
    members, _ = cohort_and_series
    sim = Simulator(members, lambda *_: None, auto_scenarios_per_hour=3600)      # one a second, to see who is chosen
    unwell = {m.device_id for m in _unwell(members)}
    now = time.monotonic()
    settled = {d.device_id for d in sim.devices.values() if d.context == "icu" or d.gen.settled(now)}
    for _ in range(60):
        sim._maybe_auto_scenario(1.0)
    hit = {d.device_id: d.scenario for d in sim.devices.values() if d.scenario}
    assert set(hit) == settled and len(hit) >= 15 and not unwell & set(hit)      # everyone settled, nobody recovering
    for device_id, sc in hit.items():
        m = sim.devices[device_id].member
        assert sc.spec.name == {"Sepsis": "sepsis", "CHF": "chf_decompensation",
                                "COPD": "copd_exacerbation"}.get(m.condition, sc.spec.name)
        assert 1200 <= sc.duration_s <= 2400 and 1800 <= sc.recover_s <= 2700
        end = sc.started + sc.duration_s
        assert sc.intensity(end - 1) == 1.0 and 0.4 < sc.intensity(end + sc.recover_s / 2) < 0.6   # fades, not stops
    assert not Simulator(members, lambda *_: None)._maybe_auto_scenario(1.0)     # off unless a rate is given


def test_recorded_data_is_only_played_back_in_the_offline_evaluation(cohort_and_series):
    """With replay=True (rpm-eval) a home patient's readings follow their recorded course, within sensor noise."""
    m = next(x for x in build_cohort(TrainingData(DATA), ANCHOR, replay=True)
             if x.care_setting == "home" and x.origin == "synthetic")
    sim = Simulator([m], lambda *_: None, replay=True)
    gen = sim.devices[m.device_id].gen
    assert isinstance(gen, TrajectoryWearable)
    for hours in (0.0, 3.0, 30.0, 71.5, 200.0):
        want = trajectory_at(m.points, hours)
        got = gen.sample(gen.t0 + (hours - gen.start_h) * 3600, 12, 30, {}, False, False, force_spo2=True).metrics
        assert got["heart_rate"]["mean"] == pytest.approx(want["heart_rate"], abs=5)
        assert got["spo2"]["value"] == pytest.approx(want["spo2"], abs=1.5)
        assert got["resp_rate"]["value"] == pytest.approx(want["resp_rate"], abs=2)
    assert trajectory_at(m.points, 200.0) == trajectory_at(m.points, 72.0)   # held at the end of the data


def test_scenario_ramps_heart_rate(cohort_and_series):
    sim, sent = make_sim(cohort_and_series)
    dev = next(d for d in sim.devices.values() if d.context == "home")
    before = []
    for _ in range(5):
        before.append(dev.gen.sample(dev.gen.t0, 12, 30, {}, False, False).metrics["heart_rate"]["mean"])
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
    dev = next(d for d in sim.devices.values() if d.context == "home")
    sim.on_message(f"rpm/v1/dev/{dev.device_id}/cmd",
                   Command(cmd="SET_PROFILE", args={"profile": "critical"}).to_json().encode())
    assert dev.window_s == 5 and dev.profile == "critical"


def test_control_topic_starts_scenario(cohort_and_series):
    sim, _ = make_sim(cohort_and_series)
    pid = cohort_and_series[0][0].patient_id
    sim.on_message("rpm/v1/sim/control",
                   json.dumps({"action": "scenario", "target": pid, "scenario": "sepsis"}).encode())
    assert sim.resolve(pid).scenario.spec.name == "sepsis"


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
    sim = Simulator([fast, slow], lambda *_: None)
    for m in (fast, slow):
        sim.start_scenario(m.patient_id, "bradycardia")
        dev = sim.devices[m.device_id]
        dev.scenario.started -= 120  # past the 60 s ramp: full effect
        deltas, *_ = sim._deltas(dev, time.monotonic())
        assert dev.gen.b["heart_rate"] + deltas["heart_rate"] == pytest.approx(38)


def _by_patient(sim, sent, patient_id):
    sent.clear()
    for d in sim.devices.values():
        d.next_due = 0
    sim.tick()
    out = [Telemetry.model_validate_json(p) for _, p in sent]
    return [t for t in out if t.device_id.endswith(patient_id)]


def test_icu_monitor_only_while_admitted_and_wearable_only_at_home(cohort_and_series):
    sim, sent = make_sim(cohort_and_series)
    icu = next(m for m in cohort_and_series[0] if m.care_setting == "icu")
    before = _by_patient(sim, sent, icu.patient_id)
    assert [(t.source, t.context, t.device_id) for t in before] == [("icu_monitor", "icu", icu.device_id)]

    sim.on_message("rpm/v1/sim/control", json.dumps({"action": "discharge", "target": icu.patient_id}).encode())
    after = _by_patient(sim, sent, icu.patient_id)
    assert [(t.source, t.context) for t in after] == [("simulator", "home")]   # the ICU monitor is gone
    assert after[0].device_id == f"sim-{icu.patient_id}" and after[0].metrics.activity is not None

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
