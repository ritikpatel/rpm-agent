"""Device -> patient resolution. The mapping lives only server-side (no PHI on the wire)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from psycopg_pool import ConnectionPool

_SQL = """
SELECT d.patient_id, p.care_setting, p.primary_condition, p.hypercapnic_resp_failure,
       p.on_supplemental_o2, p.thresholds
FROM device_assignments d JOIN patients p USING (patient_id)
WHERE d.device_id = %s AND d.active_to IS NULL AND p.monitoring_active
"""


@dataclass(frozen=True)
class PatientRef:
    patient_id: str
    context: str
    condition: str | None
    hypercapnic: bool
    on_o2: bool
    thresholds: dict = field(default_factory=dict)


class DeviceRegistry:
    def __init__(self, pool: ConnectionPool, ttl_s: float = 60, negative_ttl_s: float = 15):
        self.pool = pool
        self.ttl_s = ttl_s
        self.negative_ttl_s = negative_ttl_s
        self._cache: dict[str, tuple[float, PatientRef | None]] = {}
        self._lock = threading.Lock()

    def resolve(self, device_id: str) -> PatientRef | None:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(device_id)
        if hit and hit[0] > now:
            return hit[1]
        with self.pool.connection() as conn:
            row = conn.execute(_SQL, (device_id,)).fetchone()
        ref = PatientRef(row[0], row[1], row[2], row[3], row[4], row[5] or {}) if row else None
        with self._lock:
            self._cache[device_id] = (now + (self.ttl_s if ref else self.negative_ttl_s), ref)
        return ref

    def invalidate(self, patient_id: str) -> None:
        """Forget cached devices of a patient whose care setting or assignments just changed."""
        with self._lock:
            for device_id in [d for d, (_, ref) in self._cache.items() if ref is None or ref.patient_id == patient_id]:
                del self._cache[device_id]

    def patient(self, patient_id: str) -> PatientRef | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT patient_id, care_setting, primary_condition, hypercapnic_resp_failure, on_supplemental_o2, "
                "thresholds FROM patients WHERE patient_id = %s", (patient_id,)).fetchone()
        return PatientRef(row[0], row[1], row[2], row[3], row[4], row[5] or {}) if row else None
