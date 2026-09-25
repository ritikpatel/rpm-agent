"""Seeds the operational registry (patients, device assignments, status rows) from the same cohort."""

from __future__ import annotations

from datetime import UTC

import psycopg
from psycopg.types.json import Jsonb

from rpm_mimic import CohortMember

_UPSERT_PATIENT = """
INSERT INTO patients (patient_id, subject_id, display_name, sex, age, primary_condition, care_setting, discharge_at,
                      icu_admit_at, hypercapnic_resp_failure, on_supplemental_o2, thresholds, monitoring_active,
                      los_days, prior_admissions_12m, drg_severity, discharge_disposition)
VALUES (%(patient_id)s, %(subject_id)s, %(display_name)s, %(sex)s, %(age)s, %(condition)s, %(care_setting)s,
        %(discharge_at)s, %(icu_admit_at)s, %(hypercapnic)s, false, %(thresholds)s, true,
        %(los_days)s, %(prior_admissions_12m)s, %(drg_severity)s, %(discharge_disposition)s)
ON CONFLICT (patient_id) DO UPDATE SET
    display_name = EXCLUDED.display_name, sex = EXCLUDED.sex, age = EXCLUDED.age,
    primary_condition = EXCLUDED.primary_condition, care_setting = EXCLUDED.care_setting,
    discharge_at = EXCLUDED.discharge_at, icu_admit_at = EXCLUDED.icu_admit_at,
    hypercapnic_resp_failure = EXCLUDED.hypercapnic_resp_failure, monitoring_active = true,
    los_days = EXCLUDED.los_days, prior_admissions_12m = EXCLUDED.prior_admissions_12m,
    drg_severity = EXCLUDED.drg_severity, discharge_disposition = EXCLUDED.discharge_disposition, updated_at = now()
"""


def _utc(ts):
    return ts.replace(tzinfo=UTC) if ts is not None and ts.tzinfo is None else ts


def assign_device(conn: psycopg.Connection, device_id: str, patient_id: str, kind: str) -> None:
    """Idempotent: keeps an identical active assignment, otherwise closes old ones and opens a new one."""
    row = conn.execute("SELECT patient_id FROM device_assignments WHERE device_id=%s AND active_to IS NULL",
                       (device_id,)).fetchone()
    if row and row[0] == patient_id:
        return
    conn.execute("UPDATE device_assignments SET active_to = now() WHERE device_id=%s AND active_to IS NULL",
                 (device_id,))
    conn.execute("INSERT INTO device_assignments (device_id, patient_id, device_kind) VALUES (%s, %s, %s)",
                 (device_id, patient_id, kind))


def seed(database_url: str, members: list[CohortMember], demo: tuple[str, str] | None = None) -> dict[str, int]:
    """demo = (patient_id, device_id): the real Galaxy Watch takes over that patient; the sim device is retired."""
    with psycopg.connect(database_url) as conn, conn.transaction():
        for m in members:
            conn.execute(_UPSERT_PATIENT, {
                "patient_id": m.patient_id, "subject_id": m.subject_id, "display_name": m.display_name,
                "sex": m.sex, "age": m.age, "condition": m.condition, "care_setting": m.care_setting,
                "discharge_at": _utc(m.discharge_at), "icu_admit_at": _utc(m.icu_admit_at),
                "hypercapnic": m.hypercapnic, "thresholds": Jsonb({}), "los_days": m.los_days,
                "prior_admissions_12m": m.prior_admissions_12m, "drg_severity": m.drg_severity,
                "discharge_disposition": m.discharge_disposition,
            })
            if demo and m.patient_id == demo[0]:
                conn.execute("UPDATE device_assignments SET active_to = now() "
                             "WHERE device_id=%s AND active_to IS NULL", (m.device_id,))
                assign_device(conn, demo[1], m.patient_id, "watch")
                device = demo[1]
            else:
                assign_device(conn, m.device_id, m.patient_id, "bedside" if m.care_setting == "icu" else "simulator")
                device = m.device_id
            conn.execute("INSERT INTO patient_status (patient_id, device_id) VALUES (%s, %s) "
                         "ON CONFLICT (patient_id) DO UPDATE SET device_id = EXCLUDED.device_id",
                         (m.patient_id, device))
        n = conn.execute("SELECT count(*) FROM device_assignments WHERE active_to IS NULL").fetchone()[0]
    return {"patients": len(members), "active_devices": n}
