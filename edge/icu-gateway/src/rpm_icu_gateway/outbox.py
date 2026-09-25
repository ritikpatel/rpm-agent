"""The gateway's local SQLite store: store-and-forward outbox, per-device sequence numbers and per-bed state (cloud
config, GCS baseline). Survives gateway restarts and power loss.

Same contract as the phone gateway's outbox: events jump the queue, telemetry is FIFO, and when full the oldest
telemetry is dropped first (events are never dropped for telemetry). Thread-safe: the MLLP loop adds, the uplink
thread drains.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, priority INT NOT NULL,
                                   created REAL NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS outbox_order ON outbox (priority DESC, id);
CREATE TABLE IF NOT EXISTS seq (device_id TEXT PRIMARY KEY, value INT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Outbox:
    def __init__(self, path: str | Path, capacity: int = 50_000):
        self.capacity = capacity
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)

    def add(self, kind: str, payload: str, priority: int = 0, created: float | None = None) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute("INSERT INTO outbox (kind, priority, created, payload) VALUES (?,?,?,?)",
                             (kind, priority, created or time.time(), payload))
            over = self._db.execute("SELECT count(*) FROM outbox").fetchone()[0] - self.capacity
            if over > 0:
                self._db.execute("DELETE FROM outbox WHERE id IN (SELECT id FROM outbox WHERE kind='telemetry' "
                                 "ORDER BY id LIMIT ?)", (over,))
            self._db.execute("COMMIT")

    def peek(self, limit: int = 100) -> list[tuple[int, str, float, str]]:
        with self._lock:
            return self._db.execute("SELECT id, kind, created, payload FROM outbox ORDER BY priority DESC, id "
                                    "LIMIT ?", (limit,)).fetchall()

    def remove(self, ids: list[int]) -> None:
        if ids:
            with self._lock:
                self._db.execute(f"DELETE FROM outbox WHERE id IN ({','.join('?' * len(ids))})", ids)

    def size(self) -> int:
        with self._lock:
            return self._db.execute("SELECT count(*) FROM outbox").fetchone()[0]

    def next_seq(self, device_id: str) -> int:
        """Monotonic per device across restarts, so the cloud's gap detection stays meaningful."""
        with self._lock:
            return self._db.execute("INSERT INTO seq (device_id, value) VALUES (?, 1) ON CONFLICT (device_id) "
                                    "DO UPDATE SET value = value + 1 RETURNING value", (device_id,)).fetchone()[0]

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET "
                             "value = excluded.value", (key, value))

    def close(self) -> None:
        with self._lock:
            self._db.close()
