"""Outputs: Postgres (vitals, status, events, rule state, EHR changes: one transaction per batch) and care-api, which
opens alerts, notifies and queues agent work for the clinical events handed to it."""

from __future__ import annotations

import logging
from typing import Protocol

import httpx
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from rpm_core.cohort import sim_device_id
from rpm_core.contracts import ClinicalEvent

log = logging.getLogger(__name__)

VITAL_COLUMNS = (
    "patient_id", "device_id", "ts", "msg_id", "source", "context", "heart_rate", "hr_min", "hr_max", "hrv_rmssd",
    "spo2", "resp_rate", "sbp", "dbp", "temp", "skin_temp", "gcs", "steps", "activity", "quality", "news2",
    "news2_band", "news2_partial", "edge_flags", "buffered", "late", "edge_sent", "ingest_at", "processed_at",
)
_INSERT_VITALS = (
    f"INSERT INTO vitals ({', '.join(VITAL_COLUMNS)}) VALUES ({', '.join('%(' + c + ')s' for c in VITAL_COLUMNS)}) "
    "ON CONFLICT (msg_id, ts) DO NOTHING"
)
_UPSERT_STATUS = """
INSERT INTO patient_status (patient_id, device_id, last_seen, latest, news2, news2_band, news2_partial, news2_detail,
                            updated_at)
VALUES (%(patient_id)s, %(device_id)s, %(last_seen)s, %(latest)s, %(news2)s, %(news2_band)s, %(news2_partial)s,
        %(news2_detail)s, now())
ON CONFLICT (patient_id) DO UPDATE SET
    device_id = EXCLUDED.device_id,
    last_seen = GREATEST(patient_status.last_seen, EXCLUDED.last_seen),
    latest = EXCLUDED.latest,
    news2 = COALESCE(EXCLUDED.news2, patient_status.news2),
    news2_band = COALESCE(EXCLUDED.news2_band, patient_status.news2_band),
    news2_partial = COALESCE(EXCLUDED.news2_partial, patient_status.news2_partial),
    news2_detail = COALESCE(EXCLUDED.news2_detail, patient_status.news2_detail),
    updated_at = now()
"""
_INSERT_EVENT = """
INSERT INTO clinical_events (event_id, patient_id, device_id, type, severity, requires_alert, occurred_at,
                             detected_at, summary, payload)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (event_id) DO NOTHING
"""


_UPSERT_RULES = """
INSERT INTO stream_state (patient_id, rule_state, updated_at) VALUES (%s, %s, now())
ON CONFLICT (patient_id) DO UPDATE SET rule_state = EXCLUDED.rule_state, updated_at = now()
"""
_RESET_STATUS = ("UPDATE patient_status SET latest = '{}'::jsonb, news2 = NULL, news2_band = NULL, "
                 "news2_partial = NULL, news2_detail = NULL, last_seen = NULL, device_id = %s, updated_at = now() "
                 "WHERE patient_id = %s")
_ASSIGN = """
INSERT INTO device_assignments (device_id, patient_id, device_kind)
SELECT %(device)s, %(patient)s, %(kind)s
WHERE NOT EXISTS (SELECT 1 FROM device_assignments WHERE device_id = %(device)s AND active_to IS NULL)
  AND NOT EXISTS (SELECT 1 FROM device_assignments WHERE patient_id = %(patient)s AND active_to IS NULL
                  AND device_kind = ANY(%(kinds)s))
"""


class PostgresSink:
    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    def write(self, vitals: list[dict], status: dict[str, dict], events: list[ClinicalEvent],
              rule_states: dict[str, str] | None = None, ehr: list[dict] | None = None) -> None:
        if not (vitals or status or events or rule_states or ehr):
            return
        with self.pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            for e in ehr or []:
                self._ehr(cur, e)
            if vitals:
                cur.executemany(_INSERT_VITALS, [
                    {**v, "quality": Jsonb(v["quality"]) if v.get("quality") else None} for v in vitals
                ])
            if status:
                cur.executemany(_UPSERT_STATUS, [
                    {**s, "latest": Jsonb(s["latest"]),
                     "news2_detail": Jsonb(s["news2_detail"]) if s.get("news2_detail") else None}
                    for s in status.values()
                ])
            if events:
                cur.executemany(_INSERT_EVENT, [
                    (str(e.event_id), e.patient_id, e.device_id, e.type, e.severity, e.requires_alert, e.occurred_at,
                     e.detected_at, e.summary, Jsonb(e.to_dict()))
                    for e in events
                ])
            if rule_states:
                cur.executemany(_UPSERT_RULES, list(rule_states.items()))

    @staticmethod
    def _ehr(cur, e: dict) -> None:
        """Apply one EHR event exactly once (the insert is the idempotency guard)."""
        pid, kind, at, data = e["patient_id"], e["type"], e["occurred_at"], e["data"]
        if not cur.execute(
                "INSERT INTO ehr_events (event_id, patient_id, type, occurred_at, payload) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (event_id) DO NOTHING RETURNING 1",
                (e["event_id"], pid, kind, at, Jsonb(data))).fetchone():
            return
        if kind == "lab":
            cur.execute(
                "INSERT INTO labs (patient_id, ts, code, name, value, unit, flag, fhir_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (fhir_id) DO NOTHING",
                (pid, at, str(data.get("code", "")), str(data.get("name") or data.get("code") or "lab"),
                 data.get("value"), data.get("unit"), data.get("flag"), data.get("fhir_id")))
            return
        setting = "icu" if kind == "admission" else "home"
        if kind == "admission":
            cur.execute("UPDATE patients SET care_setting = 'icu', icu_admit_at = %s, discharge_at = NULL, "
                        "updated_at = now() WHERE patient_id = %s", (at, pid))
            cur.execute("UPDATE device_assignments SET active_to = now() WHERE patient_id = %s AND active_to IS NULL "
                        "AND device_kind <> 'icu_monitor'", (pid,))
            kinds, kind_new = ["icu_monitor"], "icu_monitor"
        else:
            cur.execute("UPDATE patients SET care_setting = 'home', discharge_at = %s, updated_at = now(), "
                        "discharge_disposition = COALESCE(%s, discharge_disposition), "
                        "los_days = COALESCE(%s, los_days) WHERE patient_id = %s",
                        (at, data.get("disposition"), data.get("los_days"), pid))
            cur.execute("UPDATE device_assignments SET active_to = now() WHERE patient_id = %s AND active_to IS NULL "
                        "AND device_kind = 'icu_monitor'", (pid,))
            kinds, kind_new = ["watch", "simulator"], "simulator"
        device = data.get("device_id") or sim_device_id(pid, setting)
        cur.execute(_ASSIGN, {"device": device, "patient": pid, "kind": kind_new, "kinds": kinds})
        cur.execute(_RESET_STATUS, (device, pid))
        cur.execute("DELETE FROM stream_state WHERE patient_id = %s", (pid,))


class Emitter(Protocol):
    def emit(self, events: list[ClinicalEvent]) -> None: ...


class CareApiEmitter:
    """Hands clinical events to care-api in one call. care-api records them against the patient's alert, pages the
    care team for those that require it and queues agent work; none of that waits on (or depends on) the LLM.
    A failure raises, so the batch is retried rather than an alert lost."""

    def __init__(self, base_url: str, token: str, timeout_s: float = 10):
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s,
                                 headers={"X-Service-Token": token, "X-Service-Name": "stream-processor"})

    def emit(self, events: list[ClinicalEvent]) -> None:
        if not events:
            return
        r = self.http.post("/internal/events", json={"events": [e.to_dict() for e in events]})
        r.raise_for_status()
