"""Alert service: consumes rule-level alerts from SQS (never waits on the agents), keeps one active alert per
patient, escalates unacknowledged alerts on a timer and delivers notifications through a transactional outbox."""

from __future__ import annotations

import json
import logging
import signal
import threading
import time

import redis
from prometheus_client import Counter, Histogram, start_http_server

from rpm_alerts import (
    AlertStore,
    CareTeam,
    ConsoleChannel,
    EmailChannel,
    EscalationPolicy,
    Notifier,
    SmsChannel,
    WatchChannel,
    WebhookChannel,
)
from rpm_common.aws import client
from rpm_common.db import make_pool, migrate, wait_for_db
from rpm_common.devices import DeviceCommander
from rpm_common.live import publish
from rpm_common.logging import setup_logging
from rpm_common.settings import BaseServiceSettings
from rpm_contracts import ClinicalEvent

log = logging.getLogger("alert_service")

OPENED = Counter("rpm_alerts_opened_total", "Alerts opened", ["severity"])
ATTACHED = Counter("rpm_alert_events_attached_total", "Events attached to an active alert")
RAISED = Counter("rpm_alert_severity_raised_total", "Alerts whose severity was raised")
ESCALATED = Counter("rpm_alert_escalations_total", "Timer-driven escalations", ["tier"])
NOTIFIED = Counter("rpm_notifications_total", "Notifications", ["channel", "status"])
NOTIFY_LATENCY = Histogram("rpm_sensor_to_notification_seconds", "Sensor window end -> notification hand-off",
                           ["channel"], buckets=(0.5, 1, 2, 3, 5, 8, 13, 21, 34, 60, 120, 300, 600))


class AlertSettings(BaseServiceSettings):
    service_name: str = "alert-service"
    escalation_policy_file: str | None = None
    care_team_file: str | None = None
    escalation_timeout_scale: float = 1.0
    console_url: str = "http://localhost:8000"
    email_backend: str = "log"          # ses | smtp | log
    email_sender: str = "alerts@rpm.local"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    sms_backend: str = "log"            # sns | log
    device_command_mode: str = "mqtt"   # mqtt | iot | disabled
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    iot_endpoint: str | None = None
    tick_s: float = 1.0
    run_migrations: bool = True


def build(cfg: AlertSettings):
    pool = make_pool(cfg.database_url, max_size=6)
    policy = EscalationPolicy.load(cfg.escalation_policy_file, cfg.escalation_timeout_scale)
    commander = DeviceCommander(mode=cfg.device_command_mode, region=cfg.aws_region, iot_endpoint=cfg.iot_endpoint,
                                mqtt_host=cfg.mqtt_host, mqtt_port=cfg.mqtt_port)
    channels = {
        "console": ConsoleChannel(),
        "email": EmailChannel(cfg.email_backend, cfg.email_sender, cfg.smtp_host, cfg.smtp_port, cfg.aws_region),
        "sms": SmsChannel(cfg.sms_backend, cfg.aws_region),
        "webhook": WebhookChannel(),
        "watch": WatchChannel(commander),
    }
    notifier = Notifier(policy, CareTeam.load(cfg.care_team_file), channels, cfg.console_url)
    return pool, AlertStore(pool, policy), notifier


def alert_summary(store: AlertStore, alert_id) -> dict | None:
    a = store.get(alert_id)
    if not a:
        return None
    keep = ("alert_id", "patient_id", "status", "severity", "tier", "title", "event_types", "event_count", "opened_at",
            "updated_at", "acknowledged_at", "acknowledged_by", "brief_status", "display_name", "care_setting",
            "primary_condition", "next_escalation_at")
    return {k: a.get(k) for k in keep}


def main() -> None:
    cfg = AlertSettings()
    setup_logging(cfg.service_name, cfg.log_level)
    start_http_server(cfg.metrics_port)
    wait_for_db(cfg.database_url)
    if cfg.run_migrations:
        migrate(cfg.database_url)
    pool, store, notifier = build(cfg)
    r = redis.Redis.from_url(cfg.redis_url)
    sqs = client("sqs", cfg.aws_region, cfg.sqs_endpoint)
    queue_url = sqs.get_queue_url(QueueName=cfg.sqs_alert_intake_queue)["QueueUrl"]
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    wake = threading.Event()  # intake nudges the dispatcher so first pages go out immediately

    def send(alert_row: dict, tier: int) -> list[dict]:
        records = notifier.send(alert_row, tier)
        for n in records:
            NOTIFIED.labels(n["channel"], n["status"]).inc()
            if n["status"] == "sent" and n.get("latency_ms") is not None:
                NOTIFY_LATENCY.labels(n["channel"]).observe(n["latency_ms"] / 1000)
        return records

    def intake() -> None:
        while not stop.is_set():
            try:
                resp = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=10,
                                           VisibilityTimeout=30)
            except Exception:
                log.exception("receive failed")
                stop.wait(2)
                continue
            for m in resp.get("Messages", []):
                try:
                    ev = ClinicalEvent.model_validate(json.loads(m["Body"]))
                    res = store.ingest_event(ev)
                    if res is not None:
                        if res.created:
                            OPENED.labels(ev.severity).inc()
                        else:
                            ATTACHED.inc()
                        if res.severity_raised:
                            RAISED.inc()
                        wake.set()
                        publish(r, {"type": "alert", "alert": alert_summary(store, res.alert_id)})
                    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=m["ReceiptHandle"])
                except Exception:
                    log.exception("alert intake failed; message will be redelivered")

    def ticker() -> None:
        while not stop.is_set():
            try:
                for row in store.run_due_escalations():
                    ESCALATED.labels(str(row["tier"])).inc()
                    publish(r, {"type": "alert", "alert": alert_summary(store, row["alert_id"])})
                for row in store.dispatch_pending(send):
                    publish(r, {"type": "alert", "alert": alert_summary(store, row["alert_id"])})
            except Exception:
                log.exception("escalation/dispatch tick failed")
            wake.wait(cfg.tick_s)
            wake.clear()

    threading.Thread(target=intake, name="intake", daemon=True).start()
    threading.Thread(target=ticker, name="ticker", daemon=True).start()
    log.info("alert service started", extra={"queue": cfg.sqs_alert_intake_queue,
                                             "timeout_scale": cfg.escalation_timeout_scale,
                                             "email": cfg.email_backend, "sms": cfg.sms_backend})
    while not stop.wait(1):
        pass
    time.sleep(0.5)
    pool.close()


if __name__ == "__main__":
    main()
