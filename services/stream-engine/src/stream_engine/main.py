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

from . import metrics as M
from .kinesis import KinesisPoller, PgCheckpoints
from .processor import Processor
from .registry import DeviceRegistry
from .sinks import PostgresSink, SqsEmitter
from .state import RedisState
from .watchdog import partition_loop, silent_device_loop

log = logging.getLogger("stream_engine")


class EngineSettings(BaseServiceSettings):
    service_name: str = "stream-engine"
    consumer_name: str = "stream-engine"
    start_position: str = "LATEST"  # LATEST | TRIM_HORIZON (first run only; afterwards checkpoints win)
    batch_limit: int = 500
    idle_sleep_s: float = 0.5
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
    kinesis = client("kinesis", cfg.aws_region, cfg.kinesis_endpoint)
    sqs = client("sqs", cfg.aws_region, cfg.sqs_endpoint)

    processor = Processor(DeviceRegistry(pool), RedisState(r), PostgresSink(pool),
                          SqsEmitter(sqs, cfg.sqs_clinical_events_queue, cfg.sqs_alert_intake_queue),
                          min_quality=cfg.min_quality, suppress_s=cfg.suppress_s, gap_tolerance=cfg.gap_tolerance)
    poller = KinesisPoller(kinesis, cfg.kinesis_stream, PgCheckpoints(pool, cfg.consumer_name, cfg.kinesis_stream),
                           start_position=cfg.start_position, batch_limit=cfg.batch_limit,
                           idle_sleep_s=cfg.idle_sleep_s,
                           on_lag=lambda shard, ms: M.MILLIS_BEHIND.labels(shard).set(ms))

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    threading.Thread(target=silent_device_loop, name="watchdog", daemon=True,
                     args=(pool, processor, cfg.silent_after_s, cfg.watchdog_interval_s, stop)).start()
    threading.Thread(target=partition_loop, name="partitions", daemon=True, args=(pool, stop)).start()
    log.info("stream engine started", extra={"stream": cfg.kinesis_stream, "consumer": cfg.consumer_name})
    poller.run(processor.handle_batch, stop)
    pool.close()


if __name__ == "__main__":
    main()
