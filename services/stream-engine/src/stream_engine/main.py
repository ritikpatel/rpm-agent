from __future__ import annotations

import logging
import signal
import threading

import redis
from prometheus_client import start_http_server

from rpm_common.aws import client
from rpm_common.db import make_pool, migrate, wait_for_db
from rpm_common.logging import setup_logging
from rpm_common.settings import BaseServiceSettings
from rpm_common.sqs import SqsReader

from .consumer import SqsConsumer, depth_loop
from .processor import Processor
from .registry import DeviceRegistry
from .sinks import PostgresSink, SqsEmitter
from .state import RedisState
from .watchdog import partition_loop, silent_device_loop

log = logging.getLogger("stream_engine")


class EngineSettings(BaseServiceSettings):
    service_name: str = "stream-engine"
    batch_limit: int = 1000         # most messages processed in one transaction (and sorted together by event time)
    receive_wait_s: int = 10        # SQS long poll; must stay below the client's 15 s read timeout
    receive_parallelism: int = 4    # concurrent ReceiveMessage calls while draining a backlog
    min_quality: float = 0.6
    suppress_s: int = 900
    gap_tolerance: int = 3
    silent_after_s: int = 600
    watchdog_interval_s: int = 60
    run_migrations: bool = True


def main() -> None:
    cfg = EngineSettings()
    setup_logging(cfg.service_name, cfg.log_level)
    start_http_server(cfg.metrics_port)
    wait_for_db(cfg.database_url)
    if cfg.run_migrations:
        migrate(cfg.database_url)
    pool = make_pool(cfg.database_url, max_size=10)
    r = redis.Redis.from_url(cfg.redis_url)
    sqs = client("sqs", cfg.aws_region, cfg.sqs_endpoint)

    processor = Processor(DeviceRegistry(pool), RedisState(r), PostgresSink(pool),
                          SqsEmitter(sqs, cfg.sqs_clinical_events_queue, cfg.sqs_alert_intake_queue),
                          min_quality=cfg.min_quality, suppress_s=cfg.suppress_s, gap_tolerance=cfg.gap_tolerance)

    def healthy() -> bool:
        try:
            with pool.connection(timeout=5) as conn:
                conn.execute("SELECT 1")
            return bool(r.ping())
        except Exception:  # noqa: BLE001 - any failure means "not yet"
            return False

    reader = SqsReader(sqs, cfg.sqs_telemetry_queue, wait_s=cfg.receive_wait_s, parallelism=cfg.receive_parallelism)
    consumer = SqsConsumer(reader, batch_limit=cfg.batch_limit, healthy=healthy)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    threading.Thread(target=silent_device_loop, name="watchdog", daemon=True,
                     args=(pool, processor, cfg.silent_after_s, cfg.watchdog_interval_s, stop)).start()
    threading.Thread(target=partition_loop, name="partitions", daemon=True, args=(pool, stop)).start()
    threading.Thread(target=depth_loop, name="queue-depth", daemon=True, args=(
        {"ingest": reader, "dlq": SqsReader(sqs, f"{cfg.sqs_telemetry_queue}-dlq", parallelism=1)}, stop)).start()
    log.info("stream engine started", extra={"queue": cfg.sqs_telemetry_queue})
    consumer.run(processor.handle_batch, stop)
    reader.close()
    pool.close()


if __name__ == "__main__":
    main()
