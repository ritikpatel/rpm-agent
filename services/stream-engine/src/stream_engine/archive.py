"""Raw telemetry archive: the IoT rule's second SQS queue -> gzip NDJSON objects in the S3 lake.

Takes the place of Amazon Data Firehose, which the project's AWS plan does not include (docs/adr/0014), and keeps
its contract: one JSON record per line, gzip, `dt=YYYY-MM-DD/` prefixes, flushed every 60 s or 5 MiB.

Delivery is at-least-once. Messages are deleted from the queue only after their object is stored, so a crash or a
failed PUT archives them again after the queue's visibility timeout; readers of the lake de-duplicate on `msg_id`.
The archive has its own queue, so a slow or failing archive can never delay the real-time path.
"""

from __future__ import annotations

import gzip
import logging
import signal
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from prometheus_client import Counter, Gauge, start_http_server

from rpm_common.aws import client
from rpm_common.logging import setup_logging
from rpm_common.settings import BaseServiceSettings
from rpm_common.sqs import SqsReader

log = logging.getLogger("telemetry_archiver")

OBJECTS = Counter("rpm_archive_objects_total", "Archive objects written")
RECORDS = Counter("rpm_archive_records_total", "Records written to the archive")
BYTES = Counter("rpm_archive_bytes_total", "Compressed bytes written to the archive")
FAILURES = Counter("rpm_archive_failures_total", "Failed archive writes (the batch is redelivered by SQS)")
BUFFERED = Gauge("rpm_archive_buffered_records", "Records received and not yet written")
LAST_FLUSH = Gauge("rpm_archive_last_flush_timestamp_seconds", "Unix time of the last successful archive write")


class ArchiveSettings(BaseServiceSettings):
    service_name: str = "telemetry-archiver"
    archive_dest: str = "data/lake/telemetry"   # a directory, or s3://<bucket>/<prefix> on AWS
    archive_flush_s: float = 60                 # must stay well below the archive queue's visibility timeout
    archive_flush_bytes: int = 5 * 1024 * 1024  # uncompressed
    receive_wait_s: int = 10
    receive_parallelism: int = 2


class LakeSink:
    def __init__(self, dest: str, region: str, endpoint: str | None = None):
        self.dest = dest.rstrip("/")
        if self.dest.startswith("s3://"):
            self.bucket, _, self.prefix = self.dest[5:].partition("/")
            self.s3 = client("s3", region, endpoint)

    def put(self, key: str, body: bytes) -> str:
        if self.dest.startswith("s3://"):
            full = f"{self.prefix}/{key}" if self.prefix else key
            self.s3.put_object(Bucket=self.bucket, Key=full, Body=body, ContentType="application/gzip")
            return f"s3://{self.bucket}/{full}"
        path = Path(self.dest) / key
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_bytes(body)
        tmp.replace(path)  # readers never see a half-written object
        return str(path)


class Archiver:
    def __init__(self, reader: SqsReader, sink: LakeSink, *, flush_s: float = 60,
                 flush_bytes: int = 5 * 1024 * 1024, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.reader, self.sink = reader, sink
        self.flush_s, self.flush_bytes = flush_s, flush_bytes
        self.clock, self.now = clock, now
        self._reset()

    def _reset(self) -> None:
        self.lines: list[bytes] = []
        self.pending: list[dict] = []
        self.size = 0
        self.first_at: float | None = None
        BUFFERED.set(0)

    def due(self) -> bool:
        return bool(self.pending) and (self.size >= self.flush_bytes
                                       or self.clock() - self.first_at >= self.flush_s)

    def poll_once(self, wait_s: int | None = None) -> str | None:
        """Receive once, buffer, and write an object if one is due. Returns the object's location if written."""
        if wait_s is None:
            wait_s = self.reader.wait_s
            if self.first_at is not None:  # never sit in a long poll past the flush deadline
                wait_s = int(max(0.0, min(wait_s, self.flush_s - (self.clock() - self.first_at))))
        messages = self.reader.receive(1000, wait_s)
        if messages and self.first_at is None:
            self.first_at = self.clock()
        for m in messages:
            line = " ".join(m.get("Body", "").splitlines()).encode()  # one record per line, whatever was sent
            self.lines.append(line)
            self.size += len(line) + 1
        self.pending += messages
        BUFFERED.set(len(self.pending))
        return self.flush() if self.due() else None

    def flush(self) -> str | None:
        if not self.pending:
            return None
        at = self.now()
        key = f"dt={at:%Y-%m-%d}/telemetry-{at:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}.ndjson.gz"
        body = gzip.compress(b"\n".join(self.lines) + b"\n")
        pending, count = self.pending, len(self.pending)
        try:
            where = self.sink.put(key, body)
        except Exception:
            FAILURES.inc()
            self._reset()  # not deleted: SQS makes these messages visible again after the visibility timeout
            raise
        self._reset()
        self.reader.delete(pending)  # only now: the object is durable
        OBJECTS.inc()
        RECORDS.inc(count)
        BYTES.inc(len(body))
        LAST_FLUSH.set(time.time())
        log.info("archive object written", extra={"location": where, "records": count, "bytes": len(body)})
        return where

    def run(self, stop: threading.Event) -> None:
        self.reader.wait_ready(stop)
        backoff = 1.0
        while not stop.is_set():
            try:
                self.poll_once()
                backoff = 1.0
            except Exception:
                log.exception("archive cycle failed; the batch will be redelivered")
                stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
        try:
            self.flush()  # shutdown: write what is buffered rather than wait out the visibility timeout
        except Exception:
            log.exception("final archive write failed; the batch will be redelivered")


def main() -> None:
    cfg = ArchiveSettings()
    setup_logging(cfg.service_name, cfg.log_level)
    start_http_server(cfg.metrics_port)
    if not cfg.sqs_archive_queue:
        raise SystemExit("RPM_SQS_ARCHIVE_QUEUE is empty: nothing to archive")
    reader = SqsReader(client("sqs", cfg.aws_region, cfg.sqs_endpoint), cfg.sqs_archive_queue,
                       wait_s=cfg.receive_wait_s, parallelism=cfg.receive_parallelism)
    archiver = Archiver(reader, LakeSink(cfg.archive_dest, cfg.aws_region), flush_s=cfg.archive_flush_s,
                        flush_bytes=cfg.archive_flush_bytes)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info("telemetry archiver started", extra={"queue": cfg.sqs_archive_queue, "dest": cfg.archive_dest})
    archiver.run(stop)
    reader.close()


if __name__ == "__main__":
    main()
