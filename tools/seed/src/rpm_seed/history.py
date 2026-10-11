"""The recorded readings as each home patient's history.

The training data holds what a patient's wearable reported after discharge (72 hours of readings for the synthetic
patients, the discharge reading and five snapshots for the MIMIC ones). Monitoring starts where that data ends, so
the readings are loaded as the past: the chart shows them leading up to now, and the risk models see a real history
from the first minute. They are stored directly, not sent through the stream, so they raise no alert and page nobody.

Seeding restarts every patient's timeline at the end of their recorded data: readings a simulated device produced in
an earlier run are cleared for the cohort's patients, their current status becomes the last recorded reading (so the
worklist shows where the record stopped until a device reports), and the alert rules' working state starts afresh.
Readings from a real watch, alerts, agent runs and the audit trail are kept. Only vital signs are written: the
data's label columns never leave the training files.
"""

from __future__ import annotations

import uuid
from datetime import UTC, timedelta

import psycopg
from psycopg.types.json import Jsonb

from rpm_core.cohort import CohortMember
from rpm_core.features import News2Input, compute_news2

SOURCE = "record"
_NS = uuid.UUID("7d1c9a52-3c0e-4a8e-9b1f-5f0f6a2d4c11")
_COLUMNS = ("patient_id", "device_id", "ts", "msg_id", "source", "context", "heart_rate", "spo2", "resp_rate", "sbp",
            "temp", "news2", "news2_band", "news2_partial")


_STATUS = ("UPDATE patient_status SET device_id = %s, last_seen = %s, latest = %s, news2 = %s, news2_band = %s, "
           "news2_partial = %s, news2_detail = %s, updated_at = now() WHERE patient_id = %s")


def _score(m: CohortMember, v: dict):
    return compute_news2(News2Input(resp_rate=v.get("resp_rate"), spo2=v.get("spo2"), on_oxygen=False,
                                    sbp=v.get("sbp"), heart_rate=v.get("heart_rate"), temp=v.get("temp"),
                                    hypercapnic_resp_failure=m.hypercapnic))


def status(m: CohortMember) -> tuple:
    """The patient's current status: the last recorded reading for a patient at home; nothing yet for a patient
    whose bedside monitor has not reported."""
    if m.care_setting != "home" or m.discharge_at is None or not m.points:
        return (m.device_id, None, Jsonb({}), None, None, None, None, m.patient_id)
    hours, v = m.points[-1]
    ts = (m.discharge_at + timedelta(hours=hours)).replace(tzinfo=UTC)
    score = _score(m, v)
    latest = {k: {"v": v[k], "ts": ts.isoformat()} for k in ("heart_rate", "spo2", "resp_rate", "sbp", "temp")
              if v.get(k) is not None}
    return (m.device_id, ts, Jsonb(latest), score.total, score.band, score.partial, Jsonb(score.as_dict()),
            m.patient_id)


def rows(m: CohortMember) -> list[tuple]:
    """One row per recorded reading of a patient at home, timed so the last one falls where monitoring starts."""
    if m.care_setting != "home" or m.discharge_at is None:
        return []
    out = []
    for hours, v in m.points:
        score = _score(m, v)
        ts = (m.discharge_at + timedelta(hours=hours)).replace(tzinfo=UTC)
        out.append((m.patient_id, m.device_id, ts, uuid.uuid5(_NS, f"{m.patient_id}:{hours}"), SOURCE, "home",
                    v.get("heart_rate"), v.get("spo2"), v.get("resp_rate"), v.get("sbp"), v.get("temp"),
                    score.total, score.band, score.partial))
    return out


def load(database_url: str, members: list[CohortMember]) -> dict[str, int]:
    history = [r for m in members for r in rows(m)]
    with psycopg.connect(database_url) as conn, conn.transaction():
        conn.execute("SELECT ensure_vitals_partitions(4, 7)")   # the history reaches back three days
        cleared = conn.execute("DELETE FROM vitals WHERE patient_id = ANY(%s) AND source = ANY(%s)",
                               ([m.patient_id for m in members], [SOURCE, "simulator"])).rowcount
        with conn.cursor() as cur, cur.copy(f"COPY vitals ({', '.join(_COLUMNS)}) FROM STDIN") as copy:
            for r in history:
                copy.write_row(r)
        for m in members:
            conn.execute(_STATUS, status(m))
        conn.execute("DELETE FROM stream_state WHERE patient_id = ANY(%s)", ([m.patient_id for m in members],))
    return {"recorded_readings": len(history), "patients": len({r[0] for r in history}),
            "earlier_simulated_readings_cleared": cleared}
