"""Minimal, dependable Kinesis consumer: one thread per shard, Postgres checkpoints, at-least-once delivery.

Deliberately simple for a 1-4 shard demo stream (see docs/adr/0002). Processing is idempotent downstream
(msg_id unique in `vitals`, Redis dedupe for events), so replays after a crash are harmless.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable

from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

Handler = Callable[[list[dict]], None]


class PgCheckpoints:
    def __init__(self, pool: ConnectionPool, consumer: str, stream: str):
        self.pool, self.consumer, self.stream = pool, consumer, stream

    def get(self, shard_id: str) -> str | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT sequence_number FROM stream_checkpoints WHERE consumer=%s AND stream=%s AND shard_id=%s",
                (self.consumer, self.stream, shard_id),
            ).fetchone()
        return row[0] if row else None

    def save(self, shard_id: str, seq: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO stream_checkpoints (consumer, stream, shard_id, sequence_number) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (consumer, stream, shard_id) DO UPDATE "
                "SET sequence_number = EXCLUDED.sequence_number, updated_at = now()",
                (self.consumer, self.stream, shard_id, seq),
            )


class KinesisPoller:
    def __init__(self, kinesis, stream: str, checkpoints: PgCheckpoints, *, start_position: str = "LATEST",
                 batch_limit: int = 500, idle_sleep_s: float = 0.5, on_lag: Callable[[str, int], None] | None = None):
        self.k = kinesis
        self.stream = stream
        self.cp = checkpoints
        self.start_position = start_position
        self.batch_limit = batch_limit
        self.idle_sleep_s = idle_sleep_s
        self.on_lag = on_lag
        self._threads: dict[str, threading.Thread] = {}

    def wait_active(self, timeout_s: float = 120) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                status = self.k.describe_stream_summary(StreamName=self.stream)["StreamDescriptionSummary"]
                if status["StreamStatus"] in ("ACTIVE", "UPDATING"):
                    return
            except self.k.exceptions.ResourceNotFoundException:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"Kinesis stream {self.stream} not active")
            time.sleep(2)

    def _iterator(self, shard_id: str, after_seq: str | None) -> str:
        if after_seq:
            kw = {"ShardIteratorType": "AFTER_SEQUENCE_NUMBER", "StartingSequenceNumber": after_seq}
        else:
            kw = {"ShardIteratorType": self.start_position}
        return self.k.get_shard_iterator(StreamName=self.stream, ShardId=shard_id, **kw)["ShardIterator"]

    def _consume(self, shard_id: str, handle: Handler, stop: threading.Event) -> None:
        last_good = self.cp.get(shard_id)
        it = self._iterator(shard_id, last_good)
        log.info("shard consumer started", extra={"shard": shard_id, "resume_after": last_good})
        backoff = 0.5
        while not stop.is_set():
            try:
                resp = self.k.get_records(ShardIterator=it, Limit=self.batch_limit)
            except self.k.exceptions.ExpiredIteratorException:
                it = self._iterator(shard_id, last_good)
                continue
            except self.k.exceptions.ProvisionedThroughputExceededException:
                stop.wait(backoff)
                backoff = min(backoff * 2, 5)
                continue
            backoff = 0.5
            records = resp.get("Records", [])
            if records:
                batch = []
                for r in records:
                    try:
                        batch.append(json.loads(r["Data"]))
                    except (ValueError, UnicodeDecodeError):
                        log.warning("undecodable record skipped", extra={"shard": shard_id, "seq": r["SequenceNumber"]})
                try:
                    if batch:
                        handle(batch)
                except Exception:
                    log.exception("batch failed; will retry from last checkpoint", extra={"shard": shard_id})
                    stop.wait(2)
                    it = (self._iterator(shard_id, last_good) if last_good else
                          self.k.get_shard_iterator(StreamName=self.stream, ShardId=shard_id,
                                                    ShardIteratorType="AT_SEQUENCE_NUMBER",
                                                    StartingSequenceNumber=records[0]["SequenceNumber"])["ShardIterator"])
                    continue
                last_good = records[-1]["SequenceNumber"]
                self.cp.save(shard_id, last_good)
            if self.on_lag:
                self.on_lag(shard_id, int(resp.get("MillisBehindLatest", 0)))
            it = resp.get("NextShardIterator")
            if it is None:
                log.info("shard closed", extra={"shard": shard_id})
                return
            if not records or resp.get("MillisBehindLatest", 0) == 0:
                stop.wait(self.idle_sleep_s)  # GetRecords is limited to 5 calls/s/shard

    def run(self, handle: Handler, stop: threading.Event, rescan_s: float = 60) -> None:
        self.wait_active()
        while not stop.is_set():
            shards = self.k.list_shards(StreamName=self.stream)["Shards"]
            for s in shards:
                sid = s["ShardId"]
                t = self._threads.get(sid)
                if t is None or not t.is_alive():
                    t = threading.Thread(target=self._consume, args=(sid, handle, stop), name=f"shard-{sid}",
                                         daemon=True)
                    t.start()
                    self._threads[sid] = t
            stop.wait(rescan_s)
        for t in self._threads.values():
            t.join(timeout=10)
