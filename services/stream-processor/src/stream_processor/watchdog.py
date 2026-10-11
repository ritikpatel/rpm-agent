"""Background duties: silent-device detection and vitals partition maintenance."""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from psycopg_pool import ConnectionPool

from .processor import Processor, silent_device_finding
from .registry import PatientRef

log = logging.getLogger(__name__)

_SILENT_SQL = """
SELECT s.patient_id, s.device_id, s.last_seen, p.care_setting, p.primary_condition, p.hypercapnic_resp_failure,
       p.on_supplemental_o2, p.thresholds, EXTRACT(EPOCH FROM now() - s.last_seen) / 60 AS minutes
FROM patient_status s JOIN patients p USING (patient_id)
WHERE p.monitoring_active AND s.last_seen IS NOT NULL
  AND s.last_seen < now() - make_interval(secs => %s)
  AND s.last_seen > now() - interval '24 hours'
"""


def silent_device_loop(pool: ConnectionPool, processor: Processor, silent_after_s: int, interval_s: int,
                       stop: threading.Event, alerting: bool = False) -> None:
    while not stop.wait(interval_s):
        try:
            with pool.connection() as conn:
                rows = conn.execute(_SILENT_SQL, (silent_after_s,)).fetchall()
            for pid, device_id, last_seen, setting, cond, hyper, o2, thr, minutes in rows:
                ref = PatientRef(pid, setting, cond, hyper, o2, thr or {})
                processor.raise_finding(ref, device_id,
                                        silent_device_finding(setting, device_id, float(minutes), alerting),
                                        last_seen if isinstance(last_seen, datetime) else processor.clock())
        except Exception:
            log.exception("silent-device check failed")


def partition_loop(pool: ConnectionPool, stop: threading.Event, interval_s: int = 3600) -> None:
    while True:
        try:
            with pool.connection() as conn:
                created = conn.execute("SELECT ensure_vitals_partitions()").fetchone()[0]
            if created:
                log.info("vitals partitions created", extra={"count": created})
        except Exception:
            log.exception("partition maintenance failed")
        if stop.wait(interval_s):
            return
