"""Agent service: consumes every clinical event from SQS and runs the agent graph.

Concurrency: a small worker pool; one run per patient at a time (Redis lock) so a patient's events are handled in
order. Cost control: the events of one telemetry batch run the model once, on the most severe of them; repeated
events of the same or lower severity within `debounce_s` do not re-run it.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import redis
from prometheus_client import Counter, Histogram, start_http_server

from rpm_alerts import SEVERITY_RANK, AlertStore, EscalationPolicy, clamp_severity
from rpm_common.aws import client
from rpm_common.db import make_pool, migrate, wait_for_db
from rpm_common.devices import DeviceCommander
from rpm_common.logging import setup_logging
from rpm_common.settings import BaseServiceSettings
from rpm_contracts import ClinicalEvent
from rpm_rag import RagIndex, make_embedder

from .context import ContextBuilder
from .graph import AgentGraph, Deps, burst_lead, classify
from .llm import make_client

log = logging.getLogger("agent_service")
BURST = timedelta(seconds=5)  # one ingest batch is persisted in one transaction; its events share a detected_at
RUNS = Counter("rpm_agent_runs_total", "Agent runs", ["trigger", "status"])
RUN_SECONDS = Histogram("rpm_agent_run_seconds", "Agent run duration", ["trigger"],
                        buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120))
SKIPPED = Counter("rpm_agent_skipped_total", "Events not sent to the model", ["reason"])
TOKENS = Counter("rpm_agent_tokens_total", "LLM tokens", ["kind"])
ACTIONS = Counter("rpm_agent_actions_total", "Agent actions", ["action", "status"])


class AgentSettings(BaseServiceSettings):
    service_name: str = "agent-service"
    llm_provider: str = "offline"            # offline | anthropic | bedrock
    llm_model: str = "claude-opus-5"
    llm_effort_assess: str = "medium"
    llm_effort_brief: str = "medium"
    embeddings: str = "hash"                 # hash | titan
    model_serving_url: str | None = "http://localhost:8001"
    device_command_mode: str = "mqtt"
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    iot_endpoint: str | None = None
    debounce_s: int = 120
    workers: int = 4
    run_migrations: bool = True


def main() -> None:
    cfg = AgentSettings()
    setup_logging(cfg.service_name, cfg.log_level)
    start_http_server(cfg.metrics_port)
    wait_for_db(cfg.database_url)
    if cfg.run_migrations:
        migrate(cfg.database_url)
    pool = make_pool(cfg.database_url, max_size=cfg.workers + 4)
    r = redis.Redis.from_url(cfg.redis_url)
    llm = make_client(cfg.llm_provider, cfg.llm_model, cfg.aws_region)
    deps = Deps(
        pool=pool, store=AlertStore(pool, EscalationPolicy.load(), consumer="agent-service"),
        commander=DeviceCommander(mode=cfg.device_command_mode, region=cfg.aws_region, iot_endpoint=cfg.iot_endpoint,
                                  mqtt_host=cfg.mqtt_host, mqtt_port=cfg.mqtt_port),
        llm=llm, rag=RagIndex(pool, make_embedder(cfg.embeddings, cfg.aws_region)),
        builder=ContextBuilder(pool, cfg.model_serving_url, cfg.fhir_base_url), redis=r,
        effort_assess=cfg.llm_effort_assess, effort_brief=cfg.llm_effort_brief)
    graph = AgentGraph(deps)
    sqs = client("sqs", cfg.aws_region, cfg.sqs_endpoint)
    queue_url = sqs.get_queue_url(QueueName=cfg.sqs_clinical_events_queue)["QueueUrl"]
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    def should_skip(ev: ClinicalEvent, trigger: str) -> str | None:
        if trigger not in ("alert", "monitor"):
            return None
        key = f"rpm:agent:last:{ev.patient_id}"
        rank = SEVERITY_RANK.get(clamp_severity(ev.severity), 0) if trigger == "alert" else 0
        prev = r.get(key)
        if prev:
            p = json.loads(prev)
            if time.time() - p["at"] < cfg.debounce_s and rank <= p["rank"]:
                return "debounced"
        r.set(key, json.dumps({"rank": rank, "at": time.time()}), ex=cfg.debounce_s)
        return None

    def lead_of(ev: ClinicalEvent) -> tuple[ClinicalEvent, list[str]]:
        """The event to run on for this event's burst, plus the ids of the burst events that run will cover (the lead
        included: its own message may still be queued behind this one)."""
        with pool.connection() as c:
            rows = c.execute(
                "SELECT payload FROM clinical_events WHERE patient_id=%s AND detected_at BETWEEN %s AND %s",
                (ev.patient_id, ev.detected_at - BURST, ev.detected_at + BURST)).fetchall()
        burst = [row[0] for row in rows if classify(row[0]) in ("alert", "monitor")]
        best = burst_lead(ev.to_dict(), burst)
        lead = ev if best["event_id"] == str(ev.event_id) else ClinicalEvent.model_validate(best)
        return lead, [e["event_id"] for e in burst]

    def handle(msg: dict) -> None:
        ev = ClinicalEvent.model_validate(json.loads(msg["Body"]))
        trigger = classify(ev.to_dict())
        lock = f"rpm:agent:lock:{ev.patient_id}"
        if not r.set(lock, "1", nx=True, ex=180):
            sqs.change_message_visibility(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"], VisibilityTimeout=5)
            return  # another run for this patient is in flight; retry shortly
        try:
            covered: list[str] = []
            if r.get(f"rpm:agent:covered:{ev.event_id}"):
                reason = "covered"  # an earlier run on a more severe event from the same burst already saw this one
            else:
                if trigger in ("alert", "monitor"):
                    ev, covered = lead_of(ev)
                    trigger = classify(ev.to_dict())
                reason = should_skip(ev, trigger)
            if reason:
                SKIPPED.labels(reason).inc()
            else:
                t0 = time.monotonic()
                out = graph.run(ev)
                for event_id in covered:
                    r.set(f"rpm:agent:covered:{event_id}", "1", ex=600)
                status = "degraded" if out.get("errors") else "ok"
                RUNS.labels(trigger, status).inc()
                RUN_SECONDS.labels(trigger).observe(time.monotonic() - t0)
                for u in out.get("usage", []):
                    TOKENS.labels("input").inc(u["input"])
                    TOKENS.labels("output").inc(u["output"])
                    TOKENS.labels("cache_read").inc(u["cache_read"])
                for a in out.get("actions", []):
                    ACTIONS.labels(a["action"], a["status"]).inc()
                log.info("agent run", extra={"patient_id": ev.patient_id, "trigger": trigger, "status": status,
                                             "event": ev.type, "actions": [a["action"] for a in out.get("actions", [])],
                                             "by": out.get("assessment_by"), "errors": out.get("errors") or None,
                                             "ms": round((time.monotonic() - t0) * 1000)})
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
        except Exception:
            RUNS.labels(trigger, "failed").inc()
            log.exception("agent run failed; message will be retried", extra={"patient_id": ev.patient_id})
        finally:
            r.delete(lock)

    log.info("agent service started", extra={"provider": cfg.llm_provider if llm else "offline",
                                             "model": cfg.llm_model if llm else None, "embeddings": cfg.embeddings})
    with ThreadPoolExecutor(max_workers=cfg.workers) as workers:
        while not stop.is_set():
            try:
                resp = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=10,
                                           VisibilityTimeout=120)
            except Exception:
                log.exception("receive failed")
                stop.wait(2)
                continue
            for m in resp.get("Messages", []):
                workers.submit(handle, m)
    pool.close()


if __name__ == "__main__":
    main()
