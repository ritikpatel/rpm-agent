"""LOCAL ONLY — stands in for the AWS IoT Core topic rule so the laptop stack matches AWS:

    SELECT *, timestamp() AS ingest_ts_ms, topic(4) AS thing, topic(5) AS kind
    FROM 'rpm/v1/dev/+/+' WHERE topic(5) = 'telemetry' OR topic(5) = 'events'
    -> Kinesis rpm-telemetry, partition key = thing

On AWS this service is not deployed; the rule is created by Terraform (infra/terraform/modules/iot).
"""

from __future__ import annotations

import json
import logging
import queue
import signal
import threading
import time

import paho.mqtt.client as mqtt
from botocore.exceptions import BotoCoreError, ClientError
from prometheus_client import Counter, start_http_server

from rpm_common.aws import client
from rpm_common.logging import setup_logging
from rpm_common.settings import BaseServiceSettings

log = logging.getLogger("iot_bridge")
FORWARDED = Counter("rpm_bridge_forwarded_total", "Messages forwarded to Kinesis", ["kind"])
DROPPED = Counter("rpm_bridge_dropped_total", "Messages dropped", ["reason"])


class BridgeSettings(BaseServiceSettings):
    service_name: str = "iot-bridge"
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883


def enrich(topic: str, payload: bytes, now_ms: int) -> tuple[str, dict] | None:
    parts = topic.split("/")
    if len(parts) != 5 or parts[:3] != ["rpm", "v1", "dev"] or parts[4] not in ("telemetry", "events"):
        return None
    doc = json.loads(payload)
    if not isinstance(doc, dict):
        raise ValueError("payload is not a JSON object")
    doc.update({"ingest_ts_ms": now_ms, "thing": parts[3], "kind": parts[4]})
    return parts[3], doc


def main() -> None:
    cfg = BridgeSettings()
    setup_logging(cfg.service_name, cfg.log_level)
    start_http_server(cfg.metrics_port)
    kinesis = client("kinesis", cfg.aws_region, cfg.kinesis_endpoint)
    # ~2 KB per record: 20k keeps the bridge inside its memory limit while Kinesis is slow or down
    q: queue.Queue[tuple[str, dict]] = queue.Queue(maxsize=20_000)
    stop = threading.Event()

    def on_connect(c, _ud, _flags, reason, _props):
        log.info("connected to broker", extra={"reason": str(reason)})
        c.subscribe([("rpm/v1/dev/+/telemetry", 1), ("rpm/v1/dev/+/events", 1)])

    def on_message(_c, _ud, msg):
        try:
            item = enrich(msg.topic, msg.payload, int(time.time() * 1000))
        except (ValueError, UnicodeDecodeError):
            DROPPED.labels("invalid_json").inc()
            return
        if item:
            try:
                q.put_nowait(item)  # never block paho's network thread: that would stall MQTT for every device
            except queue.Full:
                DROPPED.labels("backpressure").inc()

    def forward() -> None:
        while not stop.is_set():
            try:
                first = q.get(timeout=0.5)
            except queue.Empty:
                continue
            batch = [first]
            while len(batch) < 500:
                try:
                    batch.append(q.get_nowait())
                except queue.Empty:
                    break
            records = [{"Data": json.dumps(d).encode(), "PartitionKey": key} for key, d in batch]
            for attempt in range(5):
                try:
                    resp = kinesis.put_records(StreamName=cfg.kinesis_stream, Records=records)
                except (BotoCoreError, ClientError) as exc:
                    # an exception here used to kill this thread silently, leaving the queue to fill for ever
                    log.warning("kinesis put failed; retrying", extra={"error": str(exc)[:200], "attempt": attempt})
                    time.sleep(min(5.0, 0.5 * 2 ** attempt))
                    continue
                if not resp.get("FailedRecordCount"):
                    break
                records = [r for r, res in zip(records, resp["Records"], strict=True) if "ErrorCode" in res]
                time.sleep(0.2 * (attempt + 1))
            else:
                DROPPED.labels("kinesis_error").inc(len(records))
                log.error("records dropped after retries", extra={"count": len(records)})
            for _, d in batch:
                FORWARDED.labels(d["kind"]).inc()

    # Persistent session (fixed id, clean_session=False, QoS 1 subscriptions): while this emulator is disconnected
    # the broker queues matching messages for it, like an IoT rule that is always subscribed. Found by the
    # broker-outage chaos experiment, which lost 75% of outage-period windows with a clean session.
    client_ = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="iot-rule-emulator", clean_session=False)
    client_.on_connect, client_.on_message = on_connect, on_message
    client_.reconnect_delay_set(1, 10)
    client_.connect_async(cfg.mqtt_host, cfg.mqtt_port, keepalive=30)
    client_.loop_start()
    worker = threading.Thread(target=forward, daemon=True)
    worker.start()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info("iot rule emulator running", extra={"stream": cfg.kinesis_stream, "broker": cfg.mqtt_host})
    stop.wait()
    client_.loop_stop()


if __name__ == "__main__":
    main()
