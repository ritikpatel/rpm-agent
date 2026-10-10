from __future__ import annotations

import logging
import signal
import threading

import httpx
from prometheus_client import start_http_server

from rpm_core.aws import client
from rpm_core.contracts import utcnow
from rpm_core.db import make_pool, migrate, wait_for_db
from rpm_core.logging import setup_logging
from rpm_core.settings import BaseServiceSettings
from rpm_core.sqs import SqsReader

from .consumer import SqsConsumer, depth_loop
from .processor import Processor, risk_finding
from .registry import DeviceRegistry
from .risk import RiskScorer
from .sinks import CareApiEmitter, PostgresSink
from .state import LocalState
from .watchdog import partition_loop, silent_device_loop

log = logging.getLogger("stream_processor")


class ProcessorSettings(BaseServiceSettings):
    service_name: str = "stream-processor"
    batch_limit: int = 1000         # most messages processed in one transaction (and sorted together by event time)
    receive_wait_s: int = 10        # SQS long poll; must stay below the client's 15 s read timeout
    receive_parallelism: int = 4    # concurrent ReceiveMessage calls while draining a backlog
    min_quality: float = 0.6
    suppress_s: int = 900
    gap_tolerance: int = 3
    silent_after_s: int = 600
    watchdog_interval_s: int = 60
    risk_min_interval_s: float = 60  # per patient, for scores triggered by vitals
    run_migrations: bool = True


def main() -> None:
    cfg = ProcessorSettings()
    setup_logging(cfg.service_name, cfg.log_level)
    start_http_server(cfg.metrics_port)
    wait_for_db(cfg.database_url)
    if cfg.run_migrations:
        migrate(cfg.database_url)
    pool = make_pool(cfg.database_url, max_size=10)
    sqs = client("sqs", cfg.aws_region, cfg.sqs_endpoint)
    registry = DeviceRegistry(pool)

    def on_high(patient_id: str, score: dict) -> None:
        ref = registry.patient(patient_id)
        if ref is not None:
            processor.raise_finding(ref, None, risk_finding(score), utcnow())

    scorer = RiskScorer(cfg.risk_service_url, cfg.service_token, on_high, min_interval_s=cfg.risk_min_interval_s)
    processor = Processor(registry, LocalState(pool), PostgresSink(pool),
                          CareApiEmitter(cfg.care_api_url, cfg.service_token), min_quality=cfg.min_quality,
                          suppress_s=cfg.suppress_s, gap_tolerance=cfg.gap_tolerance, request_scores=scorer.request)

    def healthy() -> bool:
        try:
            with pool.connection(timeout=5) as conn:
                conn.execute("SELECT 1")
            return httpx.get(f"{cfg.care_api_url.rstrip('/')}/healthz", timeout=3).status_code == 200
        except Exception:  # noqa: BLE001 - any failure means "not yet"
            return False

    reader = SqsReader(sqs, cfg.sqs_events_queue, wait_s=cfg.receive_wait_s, parallelism=cfg.receive_parallelism)
    consumer = SqsConsumer(reader, batch_limit=cfg.batch_limit, healthy=healthy)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    threading.Thread(target=scorer.run, name="risk", daemon=True, args=(stop,)).start()
    threading.Thread(target=silent_device_loop, name="watchdog", daemon=True,
                     args=(pool, processor, cfg.silent_after_s, cfg.watchdog_interval_s, stop)).start()
    threading.Thread(target=partition_loop, name="partitions", daemon=True, args=(pool, stop)).start()
    threading.Thread(target=depth_loop, name="queue-depth", daemon=True, args=(
        {"events": reader, "dlq": SqsReader(sqs, f"{cfg.sqs_events_queue}-dlq", parallelism=1)}, stop)).start()
    log.info("stream processor started", extra={"queue": cfg.sqs_events_queue})
    consumer.run(processor.handle_batch, stop)
    reader.close()
    pool.close()


if __name__ == "__main__":
    main()
