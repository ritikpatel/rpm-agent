"""Live fan-out to consoles over Postgres LISTEN/NOTIFY (care-api's WebSocket relays the channel).

NOTIFY payloads are limited to 8000 bytes, so messages stay small: vitals snapshots and ids, never documents."""

from __future__ import annotations

import json
import logging

from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

LIVE_CHANNEL = "rpm_live"
MAX_PAYLOAD = 7900


def publish(pool: ConnectionPool, *messages: dict) -> None:
    """Best effort: a missed live update is corrected by the console's next poll; it must never fail the caller."""
    if not messages:
        return
    try:
        with pool.connection() as conn:
            for m in messages:
                payload = json.dumps(m, default=str, separators=(",", ":"))
                if len(payload.encode()) > MAX_PAYLOAD:
                    payload = json.dumps({"type": m.get("type", "changed"), "patient_id": m.get("patient_id"),
                                          "truncated": True})
                conn.execute("SELECT pg_notify(%s, %s)", (LIVE_CHANNEL, payload))
    except Exception as exc:  # noqa: BLE001
        log.warning("live publish failed", extra={"error": str(exc)[:200]})
