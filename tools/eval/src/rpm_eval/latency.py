"""Alert latency from the timestamps the platform stamps at every hop.

  window_end   the wearable or monitor closed its measurement window
  edge_sent    it handed the message to the network
  ingest_at    AWS IoT Core (or the local emulator) received it
  processed_at the stream processor scored it and applied the alert rules
  notification the care team was paged (latency recorded per notification, from the sensor reading that caused it)

Backfilled (store-and-forward) and late windows are excluded: they are delayed on purpose."""

from __future__ import annotations

import os

import psycopg
from psycopg.rows import dict_row

PG = os.environ.get("RPM_DATABASE_URL", "postgresql://rpm:rpm@localhost:5432/rpm")

def _pct(p: float, expr: str, digits: int = 3) -> str:
    return f"round(percentile_cont({p}) WITHIN GROUP (ORDER BY extract(epoch FROM {expr}))::numeric, {digits})"


_HOPS = f"""
SELECT context, count(*) AS windows,
       {_pct(0.5, 'ingest_at - edge_sent')} AS uplink_p50_s,
       {_pct(0.95, 'ingest_at - edge_sent')} AS uplink_p95_s,
       {_pct(0.5, 'processed_at - ingest_at')} AS pipeline_p50_s,
       {_pct(0.95, 'processed_at - ingest_at')} AS pipeline_p95_s,
       {_pct(0.5, 'processed_at - ts')} AS sensor_to_scored_p50_s,
       {_pct(0.95, 'processed_at - ts')} AS sensor_to_scored_p95_s,
       {_pct(0.99, 'processed_at - ts')} AS sensor_to_scored_p99_s
FROM vitals WHERE processed_at > now() - make_interval(mins => %s) AND NOT buffered AND NOT late
GROUP BY context ORDER BY context
"""
_PAGES = """
SELECT channel, cause, count(*) AS pages,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) / 1000.0)::numeric, 3) AS p50_s,
       round((percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) / 1000.0)::numeric, 3) AS p95_s,
       round((max(latency_ms) / 1000.0)::numeric, 3) AS max_s
FROM notifications WHERE sent_at > now() - make_interval(mins => %s) AND status = 'sent'
GROUP BY channel, cause ORDER BY cause, channel
"""
_AGENTS = f"""
SELECT trigger, provider, count(*) AS runs,
       {_pct(0.5, 'finished_at - started_at', 1)} AS p50_s,
       {_pct(0.95, 'finished_at - started_at', 1)} AS p95_s,
       round(avg(input_tokens)) AS avg_input_tokens, round(avg(output_tokens)) AS avg_output_tokens
FROM agent_runs WHERE started_at > now() - make_interval(mins => %s) AND finished_at IS NOT NULL
GROUP BY trigger, provider ORDER BY trigger
"""


def measure(since_minutes: int = 60) -> dict:
    with psycopg.connect(PG) as conn, conn.cursor(row_factory=dict_row) as cur:
        return {"window_minutes": since_minutes,
                "sensor_to_scored": cur.execute(_HOPS, (since_minutes,)).fetchall(),
                "sensor_to_page": cur.execute(_PAGES, (since_minutes,)).fetchall(),
                "agent_runs": cur.execute(_AGENTS, (since_minutes,)).fetchall(),
                "target": "sensor reading to page, p95 under 5 seconds"}
