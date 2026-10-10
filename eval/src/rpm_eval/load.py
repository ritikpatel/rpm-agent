"""Load test against the running local stack: N extra home patients (clones of the cohort's physiology) are
registered, streamed through the real path (MQTT -> rule emulator -> Kinesis -> stream engine -> Postgres), measured,
and deactivated again.

Local numbers characterise the pipeline code on a laptop with emulators (kinesis-mock is single-process); the AWS
plan for 10k devices is in docs/evaluation-report.md.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import psycopg

from rpm_mimic import CohortMember
from rpm_simulator.engine import Simulator
from rpm_simulator.publisher import FleetPublisher, MqttConfig

PG = "postgresql://rpm:rpm@localhost:5432/rpm"
PREFIX = "load-"


def clones(members: list[CohortMember], n: int) -> list[CohortMember]:
    home = [m for m in members if m.care_setting == "home"]
    return [replace(home[i % len(home)], patient_id=f"{PREFIX}{i:05d}", device_id=f"{PREFIX}{i:05d}",
                    subject_id=f"{PREFIX}{i:05d}", display_name=f"Load {i:05d}") for i in range(n)]


def register(fleet: list[CohortMember]) -> None:
    with psycopg.connect(PG) as c, c.transaction():
        for m in fleet:
            c.execute("INSERT INTO patients (patient_id, subject_id, display_name, sex, age, primary_condition, "
                      "care_setting, monitoring_active, thresholds) VALUES (%s,%s,%s,%s,%s,%s,'home',true,'{}') "
                      "ON CONFLICT (patient_id) DO UPDATE SET monitoring_active = true",
                      (m.patient_id, m.subject_id, m.display_name, m.sex, m.age, m.condition))
            c.execute("INSERT INTO device_assignments (device_id, patient_id, device_kind) SELECT %s, %s, 'simulator' "
                      "WHERE NOT EXISTS (SELECT 1 FROM device_assignments WHERE device_id = %s AND active_to IS NULL)",
                      (m.device_id, m.patient_id, m.device_id))
            c.execute("INSERT INTO patient_status (patient_id, device_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                      (m.patient_id, m.device_id))


def deregister() -> None:
    with psycopg.connect(PG) as c, c.transaction():
        c.execute(f"UPDATE patients SET monitoring_active = false WHERE patient_id LIKE '{PREFIX}%'")
        c.execute(f"UPDATE device_assignments SET active_to = now() WHERE device_id LIKE '{PREFIX}%' "
                  "AND active_to IS NULL")
        c.execute(f"UPDATE alerts SET status = 'resolved', resolved_at = now(), resolved_by = 'load-test', "
                  f"resolution = 'load test patient' WHERE patient_id LIKE '{PREFIX}%' AND status <> 'resolved'")


def _lag_ms() -> float | None:
    try:
        text = httpx.get("http://localhost:9101/metrics", timeout=3).text
    except httpx.HTTPError:
        return None
    vals = [float(line.split()[-1]) for line in text.splitlines() if line.startswith("rpm_kinesis_millis_behind")]
    return max(vals) if vals else None


def level(members: list[CohortMember], n: int, duration_s: int = 180, window_s: int = 30) -> dict:
    fleet = clones(members, n)
    register(fleet)
    time.sleep(16)  # stream-engine negative-cache TTL for devices it has not seen yet
    shards = max(1, math.ceil(n / 400))
    pub = FleetPublisher(MqttConfig(host="localhost", port=1883, client_prefix=f"load-{n}", shards=shards))
    pub.connect()
    sim = Simulator(fleet, {}, pub.publish, window_s=window_s, speed=1.0, seed=f"load-{n}")
    stop = threading.Event()
    worker = threading.Thread(target=sim.run, args=(stop, duration_s), daemon=True)
    t0 = datetime.now(UTC)
    worker.start()
    lags = []
    while worker.is_alive():
        lag = _lag_ms()
        if lag is not None:
            lags.append(lag)
        time.sleep(5)
    t1 = datetime.now(UTC)
    pub.close()
    time.sleep(10)  # drain
    with psycopg.connect(PG) as c:
        row = c.execute(
            "SELECT count(*), count(DISTINCT device_id), "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM processed_at - ts)), "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM processed_at - ts)), "
            "percentile_cont(0.99) WITHIN GROUP (ORDER BY extract(epoch FROM processed_at - ts)) "
            f"FROM vitals WHERE device_id LIKE '{PREFIX}%%' AND ts BETWEEN %s AND %s AND NOT buffered",
            (t0, t1)).fetchone()
    deregister()
    elapsed = (t1 - t0).total_seconds()
    expected = n * elapsed / window_s
    return {"devices": n, "offered_msgs_per_s": round(n / window_s, 1), "duration_s": round(elapsed),
            "windows_stored": row[0], "devices_seen": row[1],
            "delivered_ratio": round(row[0] / expected, 3) if expected else None,
            "e2e_p50_s": round(row[2] or 0, 2), "e2e_p95_s": round(row[3] or 0, 2), "e2e_p99_s": round(row[4] or 0, 2),
            "max_consumer_lag_s": round(max(lags) / 1000, 1) if lags else None}
