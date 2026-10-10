"""Per-device and per-patient working state for the stream processor.

One processor instance owns the queue, so the state lives in memory. What must survive a restart is reloaded from
Postgres on first use: the latest vitals come from `patient_status`, the rule state (baseline history, sustained
condition timers) from `stream_state`, which the sink writes in the same transaction as the batch. Message ids are
kept in a bounded window; the `vitals` unique key is the durable guard against duplicates."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Iterable

from psycopg_pool import ConnectionPool

from rpm_core.live import publish


class LocalState:
    def __init__(self, pool: ConnectionPool | None = None, clock: Callable[[], float] = time.time,
                 dedupe_max: int = 200_000):
        self.pool = pool
        self.clock = clock
        self.dedupe_max = dedupe_max
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._max: dict[str, int] = {}
        self._latest: dict[str, dict[str, dict]] = {}
        self._rules: dict[str, str | None] = {}
        self._dirty: set[str] = set()
        self._suppress: dict[tuple[str, str], tuple[int, float]] = {}

    # ---- idempotency
    def seen(self, msg_ids: list[str]) -> set[str]:
        return {m for m in msg_ids if m in self._seen}

    def mark_seen(self, msg_ids: Iterable[str]) -> None:
        for m in msg_ids:
            self._seen[m] = None
        while len(self._seen) > self.dedupe_max:
            self._seen.popitem(last=False)

    # ---- ordering
    def swap_max(self, key: str, value: int) -> int | None:
        prev = self._max.get(key)
        if prev is None or value > prev:
            self._max[key] = value
        return prev

    # ---- latest vitals per patient: {metric: {"v": value, "ts": iso}}
    def _load_latest(self, patient_id: str) -> dict[str, dict]:
        if patient_id not in self._latest:
            row = None
            if self.pool is not None:
                with self.pool.connection() as conn:
                    row = conn.execute("SELECT latest FROM patient_status WHERE patient_id = %s",
                                       (patient_id,)).fetchone()
            self._latest[patient_id] = dict(row[0]) if row and row[0] else {}
        return self._latest[patient_id]

    def update_latest(self, patient_id: str, values: dict[str, dict]) -> None:
        self._load_latest(patient_id).update(values)

    def get_latest(self, patient_id: str) -> dict[str, dict]:
        return dict(self._load_latest(patient_id))

    def forget(self, patient_id: str) -> None:
        """Drop a patient's working state (care setting changed: ICU values must not score a home patient)."""
        self._latest[patient_id] = {}
        self._rules[patient_id] = None
        self._dirty.add(patient_id)

    # ---- rule state
    def get_rule_state(self, patient_id: str) -> str | None:
        if patient_id not in self._rules:
            row = None
            if self.pool is not None:
                with self.pool.connection() as conn:
                    row = conn.execute("SELECT rule_state FROM stream_state WHERE patient_id = %s",
                                       (patient_id,)).fetchone()
            self._rules[patient_id] = row[0] if row else None
        return self._rules[patient_id]

    def set_rule_state(self, patient_id: str, raw: str) -> None:
        self._rules[patient_id] = raw
        self._dirty.add(patient_id)

    def take_dirty_rule_states(self) -> dict[str, str]:
        """Rule states changed since the last call; the sink persists them with the batch."""
        out = {pid: self._rules[pid] or "" for pid in self._dirty}
        self._dirty.clear()
        return out

    # ---- alert-fatigue suppression: emit only if nothing of equal or higher severity went out within the TTL
    def should_emit(self, patient_id: str, event_type: str, severity_rank: int, ttl_s: int) -> bool:
        if ttl_s <= 0:
            return True
        key, now = (patient_id, event_type), self.clock()
        cur = self._suppress.get(key)
        if cur is None or cur[1] <= now or severity_rank > cur[0]:
            self._suppress[key] = (severity_rank, now + ttl_s)
            return True
        return False

    def release_emit(self, slots: Iterable[tuple[str, str]]) -> None:
        for slot in slots:
            self._suppress.pop(slot, None)

    # ---- live fan-out (care-api WebSocket)
    def publish_live(self, messages: list[dict]) -> None:
        if messages and self.pool is not None:
            publish(self.pool, *messages)
