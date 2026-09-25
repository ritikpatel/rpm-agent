"""Outputs: Postgres (vitals, status, events — one transaction per batch) and SQS (agent + alert queues)."""

from __future__ import annotations

import json
import logging

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from rpm_contracts import ClinicalEvent

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


class PostgresSink:
    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    def write(self, vitals: list[dict], status: dict[str, dict], events: list[ClinicalEvent]) -> None:
        if not (vitals or status or events):
            return
        with self.pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
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


class SqsEmitter:
    """Every clinical event goes to the agent queue; events with requires_alert also go straight to the alert
    intake queue so deterministic alerts never wait on (or depend on) the LLM."""

    def __init__(self, sqs, clinical_queue: str, alert_queue: str):
        self.sqs = sqs
        self._names = {"clinical": clinical_queue, "alert": alert_queue}
        self._urls: dict[str, str] = {}

    def _url(self, which: str) -> str:
        if which not in self._urls:
            self._urls[which] = self.sqs.get_queue_url(QueueName=self._names[which])["QueueUrl"]
        return self._urls[which]

    def _send(self, which: str, events: list[ClinicalEvent]) -> None:
        for i in range(0, len(events), 10):
            chunk = events[i:i + 10]
            resp = self.sqs.send_message_batch(QueueUrl=self._url(which), Entries=[
                {"Id": str(n), "MessageBody": json.dumps(e.to_dict()),
                 "MessageAttributes": {
                     "type": {"DataType": "String", "StringValue": e.type},
                     "severity": {"DataType": "String", "StringValue": e.severity},
                 }}
                for n, e in enumerate(chunk)
            ])
            if resp.get("Failed"):
                raise RuntimeError(f"SQS send failed for {len(resp['Failed'])} messages: {resp['Failed'][:2]}")

    def emit(self, events: list[ClinicalEvent]) -> None:
        if not events:
            return
        self._send("clinical", events)
        alerts = [e for e in events if e.requires_alert]
        if alerts:
            self._send("alert", alerts)
