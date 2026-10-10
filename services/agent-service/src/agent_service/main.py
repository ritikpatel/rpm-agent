"""Agent service: takes tasks from the agent queue and runs the four-agent workflow on each.

It holds no patient data and has no database connection: everything it reads or does goes through care-api."""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from prometheus_client import Counter, Histogram, start_http_server

from rpm_core.aws import client
from rpm_core.contracts import AgentTask
from rpm_core.logging import setup_logging
from rpm_core.settings import BaseServiceSettings

from .api import CareApi
from .graph import AgentGraph, Deps
from .llm import make_llm

log = logging.getLogger("agent_service")

RUNS = Counter("rpm_agent_runs_total", "Agent runs", ["trigger", "status"])
RUN_SECONDS = Histogram("rpm_agent_run_seconds", "Agent run duration", ["trigger"],
                        buckets=(1, 2, 5, 10, 20, 30, 45, 60, 90, 120))
TOKENS = Counter("rpm_agent_tokens_total", "Model tokens used", ["direction", "agent"])


class AgentSettings(BaseServiceSettings):
    service_name: str = "agent-service"
    llm_provider: str = "offline"            # bedrock | offline (deterministic rules, no model calls)
    model_fast: str = "au.anthropic.claude-haiku-4-5-20251001-v1:0"   # monitoring and risk agents
    model_reasoning: str = "au.anthropic.claude-sonnet-4-6"           # escalation and summary agents
    model_fallback: str | None = "amazon.nova-lite-v1:0"              # used when a model call fails
    workers: int = 2
    visibility_timeout_s: int = 600   # longer than waiting for a patient's previous run plus the run itself


def main() -> None:
    cfg = AgentSettings()
    setup_logging(cfg.service_name, cfg.log_level)
    start_http_server(cfg.metrics_port)
    llm = make_llm(cfg.llm_provider, cfg.aws_region, cfg.model_fallback)
    graph = AgentGraph(Deps(api=CareApi(cfg.care_api_url, cfg.service_token), llm=llm, model_fast=cfg.model_fast,
                            model_reasoning=cfg.model_reasoning))
    sqs = client("sqs", cfg.aws_region, cfg.sqs_endpoint)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    queue_url = None
    while queue_url is None and not stop.is_set():
        try:
            queue_url = sqs.get_queue_url(QueueName=cfg.sqs_agent_queue)["QueueUrl"]
        except Exception as exc:  # noqa: BLE001
            log.warning("agent queue not available yet", extra={"error": str(exc)[:200]})
            stop.wait(3)

    # One run per patient at a time (two runs would act on the same alert twice): a second task for the same patient
    # waits here for the first. Deferring it to the queue instead would count as a failed delivery each time and
    # dead-letter the task while a long run is still going.
    locks: dict[str, threading.Lock] = {}
    locks_guard = threading.Lock()
    slots = threading.Semaphore(cfg.workers)   # a message is only taken off the queue when a worker is free for it

    def handle(msg: dict) -> None:
        try:
            task = AgentTask.model_validate(json.loads(msg["Body"]))
        except Exception:
            log.exception("undecodable agent task dropped")
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
            return
        with locks_guard:
            patient = locks.setdefault(task.patient_id, threading.Lock())
        if not patient.acquire(timeout=cfg.visibility_timeout_s / 2):
            return  # still busy after a long wait: the message reappears when its visibility timeout ends
        t0 = time.monotonic()
        try:
            out = graph.run(task)
            status = "degraded" if out.get("errors") else "ok"
            RUNS.labels(task.trigger, status).inc()
            RUN_SECONDS.labels(task.trigger).observe(time.monotonic() - t0)
            for u in out.get("usage", []):
                TOKENS.labels("input", u["agent"]).inc(u["input"])
                TOKENS.labels("output", u["agent"]).inc(u["output"])
            log.info("agent run", extra={
                "patient_id": task.patient_id, "trigger": task.trigger, "status": status,
                "actions": [a["action"] for a in out.get("actions", [])], "by": out.get("assessment_by"),
                "errors": out.get("errors") or None, "ms": round((time.monotonic() - t0) * 1000)})
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
        except Exception:
            RUNS.labels(task.trigger, "failed").inc()
            log.exception("agent run failed; the task will be retried", extra={"patient_id": task.patient_id})
        finally:
            patient.release()

    log.info("agent service started", extra={"provider": cfg.llm_provider, "fast": cfg.model_fast,
                                             "reasoning": cfg.model_reasoning})
    with ThreadPoolExecutor(max_workers=cfg.workers) as workers:
        while not stop.is_set():
            if not slots.acquire(timeout=1):
                continue
            free = 1
            while free < cfg.workers and slots.acquire(blocking=False):
                free += 1
            try:
                messages = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=free, WaitTimeSeconds=10,
                                               VisibilityTimeout=cfg.visibility_timeout_s).get("Messages", [])
            except Exception:
                log.exception("receive failed")
                messages = []
                stop.wait(2)
            for _ in range(free - len(messages)):
                slots.release()
            for m in messages:
                workers.submit(handle, m).add_done_callback(lambda _f: slots.release())


if __name__ == "__main__":
    main()
