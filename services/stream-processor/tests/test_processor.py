"""Processor logic with in-memory fakes (no Postgres, queue or care-api needed)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from rpm_core.contracts import DeviceEvent, DeviceEventTs, Metrics, Telemetry, TelemetryTs
from rpm_core.contracts.models import EdgeInfo
from stream_processor.processor import Processor
from stream_processor.registry import PatientRef
from stream_processor.state import LocalState

NOW = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)


class FakeRegistry:
    def __init__(self, refs):
        self.refs = refs

    def resolve(self, device_id):
        return self.refs.get(device_id)

    def invalidate(self, patient_id):
        pass


class FakeState(LocalState):
    def __init__(self):
        super().__init__(pool=None, clock=lambda: NOW.timestamp())
        self.live = []

    def publish_live(self, msgs):
        self.live.extend(msgs)


class FakePg:
    def __init__(self):
        self.vitals, self.status, self.events, self.rules, self.ehr = [], {}, [], {}, []

    def write(self, vitals, status, events, rule_states=None, ehr=None):
        self.vitals += vitals
        self.status.update(status)
        self.events += events
        self.rules.update(rule_states or {})
        self.ehr += ehr or []


class FakeSqs:
    def __init__(self):
        self.clinical, self.alert = [], []

    def emit(self, events):
        self.clinical += events
        self.alert += [e for e in events if e.requires_alert]


HOME = PatientRef("p-1", "home", "CHF", False, False, {})
ICU = PatientRef("p-2", "icu", "Sepsis", False, False, {})
COPD = PatientRef("p-3", "home", "COPD", True, False, {})


@pytest.fixture
def env():
    reg = FakeRegistry({"sim-1": HOME, "sim-icu-2": ICU, "sim-3": COPD})
    st, pg, sqs = FakeState(), FakePg(), FakeSqs()
    proc = Processor(reg, st, pg, sqs, clock=lambda: NOW + timedelta(seconds=1))
    return proc, st, pg, sqs


_seq = {"n": 0}


def telemetry(device="sim-1", end=NOW, metrics=None, buffered=False, seq=None, context="home"):
    _seq["n"] += 1
    t = Telemetry(device_id=device, seq=seq if seq is not None else _seq["n"], source="simulator", context=context,
                  ts=TelemetryTs(window_start=end - timedelta(seconds=30), window_end=end,
                                 edge_sent=end + timedelta(milliseconds=200)),
                  metrics=Metrics.model_validate(metrics or {"heart_rate": {"mean": 78, "quality": 0.95}}),
                  edge=EdgeInfo(buffered=buffered))
    return {**t.to_dict(), "thing": device, "kind": "telemetry",
            "ingest_ts_ms": int((end + timedelta(milliseconds=400)).timestamp() * 1000)}


def test_normal_home_window_persists_without_events(env):
    proc, st, pg, sqs = env
    proc.handle_batch([telemetry(metrics={"heart_rate": {"mean": 78}, "spo2": {"value": 97}})])
    assert len(pg.vitals) == 1 and not pg.events
    row = pg.vitals[0]
    assert row["patient_id"] == "p-1" and row["news2"] == 0 and row["news2_partial"] is True
    assert pg.status["p-1"]["news2_band"] == "low"
    assert st.live[0]["type"] == "vitals"


def test_low_spo2_raises_alert_on_both_queues(env):
    proc, _, pg, sqs = env
    proc.handle_batch([telemetry(metrics={"spo2": {"value": 87, "quality": 0.9}})])
    assert [e.type for e in pg.events] == ["SPO2_LOW"]
    assert len(sqs.clinical) == 1 and len(sqs.alert) == 1
    ev = sqs.alert[0]
    assert ev.trace["msg_id"] and ev.news2["total"] >= 3


def test_failed_batch_does_not_swallow_its_alert_on_retry(env):
    """The queue redelivers a batch whose write failed. The first attempt already took the suppression slot for
    the alert; unless it is given back, the retry treats its own alert as a duplicate and drops it."""
    proc, _, pg, sqs = env
    rec = telemetry(metrics={"spo2": {"value": 87, "quality": 0.9}})
    write, calls = pg.write, {"n": 0}

    def flaky(*args):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("postgres restarted")
        write(*args)

    pg.write = flaky
    with pytest.raises(ConnectionError):
        proc.handle_batch([rec])
    assert not pg.events and not sqs.alert
    proc.handle_batch([rec])
    assert [e.type for e in pg.events] == ["SPO2_LOW"] and len(sqs.alert) == 1 and len(pg.vitals) == 1
    proc.handle_batch([telemetry(metrics={"spo2": {"value": 87, "quality": 0.9}})])
    assert len(sqs.alert) == 1  # delivered once: suppression is back in force


def test_duplicate_delivery_processed_once(env):
    proc, _, pg, _ = env
    rec = telemetry()
    proc.handle_batch([rec, rec])
    proc.handle_batch([rec])
    assert len(pg.vitals) == 1


def test_spoofed_device_id_rejected(env):
    proc, _, pg, _ = env
    rec = telemetry(device="sim-1")
    rec["thing"] = "sim-3"
    proc.handle_batch([rec])
    assert not pg.vitals


def test_unassigned_device_ignored(env):
    proc, _, pg, _ = env
    proc.handle_batch([telemetry(device="gw-unknown-9")])
    assert not pg.vitals


def test_suppression_and_escalation(env):
    proc, _, pg, _ = env
    proc.handle_batch([telemetry(metrics={"spo2": {"value": 88}})])
    proc.handle_batch([telemetry(end=NOW + timedelta(seconds=30), metrics={"spo2": {"value": 88}})])
    proc.handle_batch([telemetry(end=NOW + timedelta(seconds=60), metrics={"spo2": {"value": 82}})])
    assert [e.type for e in pg.events] == ["SPO2_LOW", "SPO2_CRITICAL"]


def test_copd_patient_uses_scale2_and_personal_threshold(env):
    proc, _, pg, _ = env
    proc.handle_batch([telemetry(device="sim-3", metrics={"spo2": {"value": 89}, "heart_rate": {"mean": 80}})])
    assert not pg.events and pg.vitals[0]["news2"] == 0


def test_late_window_does_not_overwrite_latest(env):
    proc, st, pg, _ = env
    proc.handle_batch([telemetry(end=NOW, metrics={"heart_rate": {"mean": 80}})])
    proc.handle_batch([telemetry(end=NOW - timedelta(minutes=5), metrics={"heart_rate": {"mean": 150}})])
    assert pg.vitals[-1]["late"] is True
    assert st.get_latest("p-1")["heart_rate"]["v"] == 80


def test_backfilled_window_is_flagged_buffered(env):
    proc, _, pg, _ = env
    proc.handle_batch([telemetry(buffered=True)])
    assert pg.vitals[0]["buffered"] is True


def test_sequence_gap_detected(env):
    proc, _, pg, _ = env
    proc.handle_batch([telemetry(seq=10)])
    proc.handle_batch([telemetry(end=NOW + timedelta(seconds=30), seq=20)])
    gap = [e for e in pg.events if e.type == "DATA_GAP"]
    assert gap and gap[0].evidence["missing"] == 9 and not gap[0].requires_alert


def test_no_gap_event_while_draining_a_backlog(env):
    """SQS hands a backlog out in rough order only: a jump in sequence numbers there is reordering, not loss."""
    proc, _, pg, _ = env
    proc.handle_batch([telemetry(seq=1, end=NOW - timedelta(minutes=5))])
    waited = telemetry(seq=9, end=NOW - timedelta(minutes=2))  # ingested 2 min before the engine's clock
    proc.handle_batch([waited])
    assert len(pg.vitals) == 2 and not [e for e in pg.events if e.type == "DATA_GAP"]
    proc.handle_batch([telemetry(seq=20, end=NOW)])  # live again: a real gap is reported
    assert [e.type for e in pg.events] == ["DATA_GAP"]


def test_icu_deterioration_scores_full_news2_high(env):
    proc, _, pg, sqs = env
    proc.handle_batch([telemetry(device="sim-icu-2", context="icu", metrics={
        "heart_rate": {"mean": 118}, "spo2": {"value": 91}, "resp_rate": {"value": 24}, "sbp": {"value": 98},
        "temp": {"value": 38.6}, "gcs": {"value": 15}})])
    row = pg.vitals[0]
    assert row["news2_partial"] is False and row["news2"] == 10
    assert "NEWS2_HIGH" in {e.type for e in sqs.alert}


def test_sos_event_is_critical(env):
    proc, _, pg, sqs = env
    ev = DeviceEvent(device_id="sim-1", seq=999, source="simulator", type="SOS",
                     ts=DeviceEventTs(occurred=NOW, edge_sent=NOW))
    proc.handle_batch([{**ev.to_dict(), "thing": "sim-1", "kind": "events", "ingest_ts_ms": int(time.time() * 1000)}])
    assert sqs.alert[0].type == "SOS" and sqs.alert[0].severity == "critical"


def test_spot_check_result_feeds_spo2_rules(env):
    proc, _, pg, _ = env
    proc.handle_batch([telemetry(end=NOW + timedelta(minutes=1))])
    ev = DeviceEvent(device_id="sim-1", seq=5000, source="simulator", type="SPOT_CHECK_RESULT",
                     ts=DeviceEventTs(occurred=NOW + timedelta(minutes=2), edge_sent=NOW + timedelta(minutes=2)),
                     data={"metric": "spo2", "value": 86, "quality": 0.9})
    proc.handle_batch([{**ev.to_dict(), "thing": "sim-1", "kind": "events", "ingest_ts_ms": 1}])
    assert "SPO2_LOW" in {e.type for e in pg.events}
    proc.handle_batch([telemetry(end=NOW + timedelta(minutes=1, seconds=30))])
    assert pg.vitals[-1]["late"] is False


def test_invalid_record_skipped_not_fatal(env):
    proc, _, pg, _ = env
    bad = telemetry()
    bad["metrics"] = {"heart_rate": {"mean": "fast"}}
    proc.handle_batch([bad, telemetry()])
    assert len(pg.vitals) == 1


def ehr(kind, pid, at=NOW, **data):
    return {"schema": "rpm.ehr_event.v1", "msg_id": f"Encounter/e-{pid}/{kind}", "type": kind, "patient_id": pid,
            "occurred_at": at.isoformat(), "data": data, "thing": "fhir", "kind": "ehr",
            "ingest_ts_ms": int(at.timestamp() * 1000)}


ICU_VITALS = {"heart_rate": {"mean": 96}, "spo2": {"value": 95}, "resp_rate": {"value": 18}, "sbp": {"value": 118},
              "temp": {"value": 37.0}, "gcs": {"value": 12}}


def test_icu_data_is_refused_for_a_patient_at_home_and_wearable_data_for_one_in_icu(env):
    proc, _, pg, _ = env
    proc.handle_batch([telemetry(device="sim-1", context="icu", metrics=ICU_VITALS),          # p-1 is at home
                       telemetry(device="sim-icu-2", context="home")])                        # p-2 is in ICU
    assert not pg.vitals and not pg.status


def test_discharge_updates_the_record_asks_for_scores_and_carries_nothing_over(env):
    proc, st, pg, emitter = env
    scored = []
    proc.request_scores = lambda pid, trigger: scored.append((pid, trigger))
    proc.handle_batch([telemetry(device="sim-icu-2", context="icu", metrics=ICU_VITALS)])
    assert st.get_latest("p-2")["gcs"]["v"] == 12
    later = NOW + timedelta(seconds=40)
    # the discharge and one more ICU window arrive together: the window after the discharge must not count
    proc.handle_batch([ehr("discharge", "p-2", NOW + timedelta(seconds=20), disposition="Home"),
                       telemetry(device="sim-icu-2", context="icu", end=later, metrics=ICU_VITALS)])
    assert [e["type"] for e in pg.ehr] == ["discharge"]
    assert [e.type for e in emitter.clinical if e.patient_id == "p-2"] == ["DISCHARGED"]
    assert ("p-2", "discharge") in scored
    assert st.get_latest("p-2") == {} and st.get_rule_state("p-2") is None      # ICU values are not carried home
    assert len([v for v in pg.vitals if v["patient_id"] == "p-2"]) == 1          # only the window before discharge


def test_lab_result_is_recorded_and_triggers_scoring(env):
    proc, _, pg, emitter = env
    scored = []
    proc.request_scores = lambda pid, trigger: scored.append((pid, trigger))
    lab = {**ehr("lab", "p-1", name="Lactate", value=4.2, unit="mmol/L", flag="H"), "msg_id": "Observation/o1/1"}
    proc.handle_batch([lab, lab])                                              # redelivered: handled once
    assert len(pg.ehr) == 1 and pg.ehr[0]["data"]["value"] == 4.2
    assert [e.type for e in emitter.clinical] == ["LAB_RESULT"] and not emitter.alert
    assert scored == [("p-1", "lab")]


def test_a_silent_device_is_recorded_but_alerts_only_when_switched_on_and_only_in_icu():
    from stream_processor.processor import silent_device_finding

    quiet = silent_device_finding("icu", "sim-icu-x", 12.0)                    # the default: a simulated demo
    assert quiet.type == "DEVICE_SILENT" and not quiet.requires_alert and quiet.severity == "low"
    assert "No data from sim-icu-x for 12 min" in quiet.summary
    real = silent_device_finding("icu", "monitor-7", 12.0, alerting=True)      # real monitors
    assert real.requires_alert and real.severity == "medium"
    assert not silent_device_finding("home", "gw-1", 12.0, alerting=True).requires_alert   # a watch taken off


# ------------------------------------------------------------------ model-confirmed alerts (patients at home)
@pytest.fixture
def gated():
    reg = FakeRegistry({"sim-1": HOME, "sim-icu-2": ICU})
    st, pg, sqs, held = FakeState(), FakePg(), FakeSqs(), []
    proc = Processor(reg, st, pg, sqs, clock=lambda: NOW + timedelta(seconds=1),
                     request_confirm=lambda pid, pending: held.append(pending))
    return proc, pg, sqs, held


def test_a_medium_home_finding_is_recorded_and_waits_for_the_models(gated):
    proc, pg, sqs, held = gated
    proc.handle_batch([telemetry(metrics={"spo2": {"value": 87, "quality": 0.9}})])
    assert [e.type for e in pg.events] == ["SPO2_LOW"] and not sqs.alert              # recorded, not paged
    assert pg.events[0].summary.endswith("(alerts only if the risk models agree)")
    assert pg.events[0].evidence["awaiting_models"]
    assert [(h["patient_id"], h["type"]) for h in held] == [("p-1", "SPO2_LOW")]
    assert "risk models" not in held[0]["summary"]                                      # the rule's own words


def test_icu_and_critical_findings_page_without_waiting(gated):
    proc, _, sqs, held = gated
    proc.handle_batch([telemetry("sim-icu-2", metrics={"spo2": {"value": 87, "quality": 0.9}}, context="icu"),
                       telemetry(metrics={"spo2": {"value": 80, "quality": 0.9}})])
    assert sorted(e.type for e in sqs.alert) == ["SPO2_CRITICAL", "SPO2_LOW"] and not held


def test_a_confirmed_finding_pages_although_its_type_was_just_recorded(gated):
    from stream_processor.processor import confirmed_finding

    proc, _, sqs, held = gated
    proc.handle_batch([telemetry(metrics={"spo2": {"value": 87, "quality": 0.9}})])
    score = {"model": "recovery_monitoring", "title": "Recovery monitoring", "probability": 0.41, "validated": True,
             "alert_threshold": 0.348, "output": {"headline": {"label": "Recovery score", "value": 38.2,
                                                               "suffix": "/ 100"}}}
    f = confirmed_finding(held[0], score)
    assert proc.raise_finding(HOME, "sim-1", f, NOW, confirmed=True)
    assert [e.type for e in sqs.alert] == ["SPO2_LOW"]
    assert sqs.alert[0].summary.endswith("confirmed by Recovery monitoring (Recovery score 38.2 / 100)")
    assert sqs.alert[0].evidence["confirmed_by"]["model"] == "recovery_monitoring"
    unanswered = confirmed_finding(held[0], None)                       # no model could answer: the rule pages
    assert unanswered.requires_alert and "risk models unavailable" in unanswered.summary


def test_the_scorer_confirms_only_when_a_confirming_model_is_at_its_threshold():
    from stream_processor.risk import RiskScorer

    def scorer(answer):
        confirmed, high = [], []
        s = RiskScorer("http://risk", "t", lambda pid, score: high.append(score["model"]),
                       on_confirmed=lambda pid, pending, score: confirmed.append(score and score["model"]))
        s._post = answer   # type: ignore[method-assign]
        s.confirm("p-1", {"type": "NEWS2_MEDIUM"})
        s._confirm_now("p-1")
        return confirmed, high

    def scores(det, rec):
        return lambda pid, trigger: [
            {"model": "early_deterioration", "band": det, "probability": 0.3, "validated": False,
             "confirms_alerts": True},
            {"model": "recovery_monitoring", "band": rec, "probability": 0.4, "validated": True,
             "confirms_alerts": True},
            {"model": "readmission_risk", "band": "high", "probability": 0.5, "confirms_alerts": False}]

    assert scorer(scores("high", "high")) == (["recovery_monitoring"], [])      # validated first; no double alert
    assert scorer(scores("high", "low")) == (["early_deterioration"], [])
    assert scorer(scores("low", "low")) == ([], [])                             # readmission never confirms

    def down(pid, trigger):
        raise ConnectionError("risk service down")
    assert scorer(down) == ([None], [])                                          # pages on the rule alone
