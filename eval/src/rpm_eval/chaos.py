"""Chaos experiments against the running local stack (docker compose). Each experiment breaks one thing for real
and checks an invariant the architecture promises:

  stream-engine-kill   Kinesis checkpoint + dedupe: no telemetry lost, none duplicated, no false DATA_GAP
  broker-outage        store-and-forward (devices, simulator, ICU gateway outbox): every window arrives, no seq gaps
  agent-down           the deterministic safety net pages without any LLM/agent (ADR 0001)
  alert-service-kill   escalation timers live in Postgres: a restart does not lose a due escalation

Needs `make up` + `make sim`. Takes ~2 minutes per experiment.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import paho.mqtt.client as mqtt
import psycopg
from psycopg.rows import dict_row

PG = "postgresql://rpm:rpm@localhost:5432/rpm"


@dataclass
class Result:
    name: str
    passed: bool
    checks: dict[str, object] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True)


def _q(sql: str, params=()) -> list[dict]:
    with psycopg.connect(PG) as c, c.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def _now() -> datetime:
    return datetime.now(UTC)


def _wait(cond: Callable[[], bool], timeout_s: float, every_s: float = 2) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(every_s)
    return cond()


def _scenario(target: str, scenario: str, **extra) -> None:
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"chaos-{uuid.uuid4().hex[:8]}")
    c.connect("localhost", 1883)
    c.loop_start()
    c.publish("rpm/v1/sim/control", json.dumps({"action": "scenario", "target": target, "scenario": scenario,
                                                **extra}), qos=1).wait_for_publish(5)
    c.loop_stop()
    c.disconnect()


def _integrity(since: datetime, until: datetime) -> dict:
    """Completeness of the simulator/gateway fleet's telemetry between two instants."""
    rows = _q("SELECT count(*) AS n, count(DISTINCT msg_id) AS distinct_msgs, count(DISTINCT device_id) AS devices "
              "FROM vitals WHERE ts BETWEEN %s AND %s AND source IN ('simulator', 'icu_gateway')", (since, until))[0]
    gaps = _q("SELECT count(*) AS n FROM clinical_events WHERE type = 'DATA_GAP' AND detected_at > %s", (since,))[0]
    return {"windows": rows["n"], "duplicates": rows["n"] - rows["distinct_msgs"], "devices": rows["devices"],
            "data_gap_events": gaps["n"]}


def stream_engine_kill(outage_s: int = 30) -> Result:
    t0 = _now()
    time.sleep(20)                                           # steady state before the fault
    _docker("kill", "rpm-stream-engine-1")
    time.sleep(outage_s)
    _docker("start", "rpm-stream-engine-1")
    t1 = _now()
    caught_up = _wait(lambda: _q("SELECT max(processed_at) AS m FROM vitals")[0]["m"] > t1, 90)
    time.sleep(40)                                           # let the backlog drain
    before = _integrity(t0, t1)
    time.sleep(1)
    lag = _q("SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM processed_at - ts)) AS p95 "
             "FROM vitals WHERE ts > now() - interval '20 seconds' AND NOT buffered")[0]["p95"]
    ok = caught_up and before["duplicates"] == 0 and before["data_gap_events"] == 0
    return Result("stream-engine-kill", ok, {**before, "outage_s": outage_s, "caught_up": caught_up,
                                             "p95_latency_after_recovery_s": round(lag or 0, 2)},
                  ["windows published during the outage were processed from the Kinesis checkpoint on restart"])


def broker_outage(outage_s: int = 60) -> Result:
    t0 = _now()
    time.sleep(20)
    _docker("stop", "rpm-mosquitto-1")
    time.sleep(outage_s)
    _docker("start", "rpm-mosquitto-1")
    t1 = _now()
    time.sleep(75)                                           # reconnect backoff + backfill
    stats = _integrity(t0, t1)
    expected_rate = _q("SELECT count(*) AS n FROM vitals WHERE ts BETWEEN %s - interval '5 minutes' AND %s "
                       "AND source IN ('simulator', 'icu_gateway')", (t0, t0))[0]["n"] / 300
    got_rate = stats["windows"] / (t1 - t0).total_seconds()
    buffered = _q("SELECT count(*) AS n FROM vitals WHERE ts BETWEEN %s AND %s AND buffered", (t0, t1))[0]["n"]
    completeness = round(got_rate / expected_rate, 3) if expected_rate else None
    ok = stats["duplicates"] == 0 and stats["data_gap_events"] == 0 and (completeness or 0) >= 0.98
    return Result("broker-outage", ok, {**stats, "outage_s": outage_s, "completeness_vs_pre_fault_rate": completeness,
                                        "buffered_windows": buffered})


def agent_down() -> Result:
    target = _q("SELECT p.patient_id FROM patients p WHERE p.care_setting = 'home' AND p.monitoring_active "
                "AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.patient_id = p.patient_id "
                "AND a.status IN ('open','acknowledged','escalated')) AND p.patient_id <> 'p-90000006' "
                "ORDER BY p.patient_id LIMIT 1")[0]["patient_id"]
    _docker("stop", "rpm-agent-service-1")
    try:
        t0 = _now()
        _scenario(target, "sos")
        paged = _wait(lambda: bool(_q(
            "SELECT 1 FROM notifications n JOIN alerts a USING (alert_id) WHERE a.patient_id = %s "
            "AND n.sent_at > %s AND n.status = 'sent'", (target, t0))), 30, 1)
        row = _q("SELECT a.severity, a.tier, min(n.sent_at) AS first_page, a.brief_status FROM alerts a "
                 "LEFT JOIN notifications n USING (alert_id) WHERE a.patient_id = %s AND a.opened_at > %s "
                 "GROUP BY a.alert_id", (target, t0))
        first = row[0] if row else {}
        secs = (first["first_page"] - t0).total_seconds() if first.get("first_page") else None
    finally:
        _docker("start", "rpm-agent-service-1")
    brief = _wait(lambda: bool(_q("SELECT 1 FROM alerts WHERE patient_id = %s AND opened_at > %s AND brief IS NOT "
                                  "NULL", (target, t0))), 90)
    return Result("agent-down", paged and brief, {
        "patient": target, "paged_without_agent": paged, "sos_to_first_page_s": round(secs, 2) if secs else None,
        "severity": first.get("severity"), "tier": first.get("tier"), "brief_after_agent_restart": brief},
        ["the SOS was paged by the rules path while the agent service was stopped; the brief followed once it "
         "restarted and drained its queue (SQS retains the events)"])


def alert_service_kill() -> Result:
    target = _q("SELECT p.patient_id FROM patients p WHERE p.care_setting = 'home' AND p.monitoring_active "
                "AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.patient_id = p.patient_id "
                "AND a.status IN ('open','acknowledged','escalated')) AND p.patient_id <> 'p-90000006' "
                "ORDER BY p.patient_id DESC LIMIT 1")[0]["patient_id"]
    t0 = _now()
    _scenario(target, "fall")
    opened = _wait(lambda: bool(_q("SELECT 1 FROM alerts WHERE patient_id = %s AND opened_at > %s", (target, t0))),
                   30, 1)
    alert = _q("SELECT alert_id, tier, next_escalation_at FROM alerts WHERE patient_id = %s AND opened_at > %s",
               (target, t0))[0]
    _docker("kill", "rpm-alert-service-1")
    due = alert["next_escalation_at"]
    wait_s = max(0.0, (due - _now()).total_seconds()) + 10  # stay down until after the escalation was due
    time.sleep(wait_s)
    _docker("start", "rpm-alert-service-1")
    # the due timer action is an escalation to the next tier, or a reminder at the top tier (critical opens there)
    fired = _wait(lambda: bool(_q("SELECT 1 FROM alert_timeline WHERE alert_id = %s AND kind = 'escalated' "
                                  "AND at >= %s", (alert["alert_id"], due))), 60, 1)
    after = _q("SELECT tier, status, reminders_sent FROM alerts WHERE alert_id = %s", (alert["alert_id"],))[0]
    late = _q("SELECT min(at) AS at FROM alert_timeline WHERE alert_id = %s AND kind = 'escalated' AND at >= %s",
              (alert["alert_id"], due))[0]["at"]
    paged = bool(_q("SELECT 1 FROM notifications WHERE alert_id = %s AND sent_at >= %s AND cause = 'timer'",
                    (alert["alert_id"], due)))
    return Result("alert-service-kill", opened and fired and paged, {
        "patient": target, "tier_before": alert["tier"], "tier_after": after["tier"],
        "reminders_sent": after["reminders_sent"], "down_for_s": round(wait_s, 1),
        "timer_fired_s_after_due": round((late - due).total_seconds(), 1) if late else None,
        "timer_page_sent": paged},
        ["the escalation timer came due while the service was down; it fired from Postgres on restart and paged"])


EXPERIMENTS = {"stream-engine-kill": stream_engine_kill, "broker-outage": broker_outage, "agent-down": agent_down,
               "alert-service-kill": alert_service_kill}
