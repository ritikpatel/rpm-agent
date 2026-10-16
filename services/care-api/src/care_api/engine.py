"""Alert engine: takes clinical events from the stream processor, keeps one active alert per patient, escalates
unacknowledged alerts on a timer, delivers notifications through a transactional outbox and queues agent work.

Nothing here waits on the agents: a page goes out as soon as the rule-level event arrives; the agents' summary and
escalation advice are attached to the alert when they finish."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace

from prometheus_client import Counter, Histogram

from rpm_core import checkin
from rpm_core.contracts import AgentTask, ClinicalEvent
from rpm_core.devices import DeviceCommander
from rpm_core.live import publish

from .alerts import (
    SEVERITY_RANK,
    AlertStore,
    CareTeam,
    ConsoleChannel,
    EmailChannel,
    EscalationPolicy,
    Notifier,
    WatchChannel,
    WebhookChannel,
    clamp_severity,
)
from .settings import CareSettings

log = logging.getLogger(__name__)

OPENED = Counter("rpm_alerts_opened_total", "Alerts opened", ["severity"])
ATTACHED = Counter("rpm_alert_events_attached_total", "Events attached to an active alert")
RAISED = Counter("rpm_alert_severity_raised_total", "Alerts whose severity was raised")
ESCALATED = Counter("rpm_alert_escalations_total", "Timer-driven escalations", ["tier"])
NOTIFIED = Counter("rpm_notifications_total", "Notifications", ["channel", "status"])
AGENT_TASKS = Counter("rpm_agent_tasks_total", "Agent tasks queued", ["trigger", "outcome"])
AUTO_RESOLVED = Counter("rpm_alerts_auto_resolved_total",
                        "Silent-device alerts closed because data returned or because they are switched off")
RETURNED_CHECK_S = 15   # how often silent-device alerts are checked against the devices' latest readings
NOTIFY_LATENCY = Histogram("rpm_page_latency_seconds",
                           "Page cause -> notification hand-off. cause=event is sensor-to-page latency; timer, agent "
                           "and manual measure dispatch delay after the escalation was due or requested",
                           ["channel", "cause"], buckets=(0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13, 21, 34, 60, 120))

SUMMARY_FIELDS = ("alert_id", "patient_id", "status", "severity", "tier", "title", "event_types", "event_count",
                  "opened_at", "updated_at", "acknowledged_at", "acknowledged_by", "brief_status", "display_name",
                  "care_setting", "primary_condition", "next_escalation_at")
# Which events start agent work when they do not open or raise an alert themselves.
TRIGGER_FOR = {"CHECKIN_REPLY": "reply", "NEWS2_RISING": "monitor", "RISK_HIGH": "monitor", "DISCHARGED": "discharge"}
TRIGGER_RANK = {"alert": 3, "reply": 2, "discharge": 2, "monitor": 1}


class AgentThrottle:
    """Bounds what the agents cost. Per patient: one run per window unless severity rose, with a longer window for
    runs that no alert started (a rising score, a risk band). A newly opened alert always gets its run: the window only
    stops repeat runs, and a new alert minutes after the last one closed is not a repeat. Overall: an optional ceiling
    on automatic runs in any 24 hours. A clinician's on-demand review and a patient's reply always pass, and nothing
    here delays a page."""

    AUTOMATIC = ("alert", "monitor", "discharge")

    def __init__(self, debounce_s: float, monitor_interval_s: float | None, daily_limit: int, runs_last_24h,
                 clock=time.monotonic):
        self.debounce_s = debounce_s
        self.monitor_interval_s = debounce_s if monitor_interval_s is None else monitor_interval_s
        self.daily_limit, self.runs_last_24h, self.clock = daily_limit, runs_last_24h, clock
        self._last: dict[str, tuple[int, float]] = {}
        self._count: tuple[float, int] | None = None   # (when counted, runs): the store is asked at most twice a minute
        self._lock = threading.Lock()

    def _runs(self, now: float) -> int:
        if self._count is None or now - self._count[0] > 30:
            self._count = (now, int(self.runs_last_24h()))
        return self._count[1]

    def admit(self, trigger: str, patient_id: str, rank: int = 0, new_alert: bool = False) -> str:
        """"queued", "debounced" or "capped"."""
        if trigger not in self.AUTOMATIC:
            return "queued"
        with self._lock:
            now = self.clock()
            if trigger != "discharge":
                prev = self._last.get(patient_id)
                window = self.monitor_interval_s if trigger == "monitor" else self.debounce_s
                if prev and now - prev[1] < window and rank <= prev[0] and not new_alert:
                    return "debounced"
            if self.daily_limit and self._runs(now) >= self.daily_limit:
                return "capped"
            if trigger != "discharge":
                self._last[patient_id] = (rank, now)
            if self._count:
                self._count = (self._count[0], self._count[1] + 1)
            return "queued"


class AlertEngine:
    def __init__(self, cfg: CareSettings, pool, sqs, commander: DeviceCommander):
        self.cfg, self.pool, self.sqs = cfg, pool, sqs
        self.policy = EscalationPolicy.load(cfg.escalation_policy_file, cfg.escalation_timeout_scale)
        if cfg.max_reminders is not None:
            self.policy = replace(self.policy, max_reminders=cfg.max_reminders)
        team = CareTeam.load(cfg.care_team_file)
        if cfg.notify_email:
            for contact in team.contacts.values():
                contact["email"] = [a.strip() for a in cfg.notify_email.split(",") if a.strip()]
        channels = {
            "console": ConsoleChannel(),
            "email": EmailChannel(cfg.email_backend, cfg.email_sender, cfg.smtp_host, cfg.smtp_port, cfg.aws_region),
            "webhook": WebhookChannel(),
            "watch": WatchChannel(commander),
        }
        self.store = AlertStore(pool, self.policy, consumer="care-api")
        self.notifier = Notifier(self.policy, team, channels, cfg.console_url)
        self._wake = threading.Event()   # intake nudges the dispatcher so first pages go out immediately
        self._queue_url: str | None = None
        self.throttle = AgentThrottle(cfg.agent_debounce_s, cfg.agent_monitor_interval_s, cfg.agent_daily_limit,
                                      self._agent_runs_last_24h)
        self._capped_logged = 0.0
        self._returned_checked = 0.0

    # ------------------------------------------------------------------ live
    def summary(self, alert_id) -> dict | None:
        a = self.store.get(alert_id)
        return {k: a.get(k) for k in SUMMARY_FIELDS} if a else None

    def changed(self, alert_id) -> None:
        publish(self.pool, {"type": "alert", "alert": self.summary(alert_id)})

    # ------------------------------------------------------------------ intake
    def ingest(self, events: list[ClinicalEvent]) -> dict:
        """Record a batch of clinical events. Events that arrived together describe one clinical change, so each
        patient gets at most one agent task per trigger, on the most severe event."""
        opened = attached = 0
        tasks: dict[tuple[str, str], AgentTask] = {}
        new_alerts: set[str] = set()
        for ev in events:
            trigger, alert_id = TRIGGER_FOR.get(ev.type), None
            if ev.requires_alert:
                res = self.store.ingest_event(ev)
                if res is None:
                    continue  # already consumed (redelivered batch)
                if res.created:
                    opened += 1
                    OPENED.labels(clamp_severity(ev.severity)).inc()
                else:
                    attached += 1
                    ATTACHED.inc()
                if res.severity_raised:
                    RAISED.inc()
                self.changed(res.alert_id)
                alert_id = str(res.alert_id)
                if res.created:
                    new_alerts.add(alert_id)
                trigger = "alert" if res.created or res.severity_raised else None
            elif ev.type == "CHECKIN_REPLY":
                self._record_reply(ev)
            elif ev.type == "RISK_HIGH" and not (ev.evidence or {}).get("validated"):
                trigger = None  # an unvalidated model is shown, but it starts no agent work and no patient contact
            if trigger == "monitor" and self.store.active_for_patient(ev.patient_id):
                trigger = None  # the patient already has an alert with the agents' work on it
            if trigger is None:
                continue
            key = (ev.patient_id, trigger)
            best = tasks.get(key)
            if best is None or _rank(ev) > _rank_dict(best.event):
                tasks[key] = AgentTask(trigger=trigger, patient_id=ev.patient_id, alert_id=alert_id,
                                       event=ev.to_dict())
        if opened or attached:
            self._wake.set()  # once per batch, so one dispatch covers every event that arrived together
        queued = 0
        for (patient_id, trigger), task in tasks.items():
            if trigger == "monitor" and (patient_id, "alert") in tasks:
                continue  # the alert run covers it
            queued += self.enqueue(task, new_alert=task.alert_id in new_alerts)
        return {"opened": opened, "attached": attached, "agent_tasks": queued}

    def _record_reply(self, ev: ClinicalEvent) -> None:
        data = ev.evidence or {}
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO messages (patient_id, direction, kind, text, sender, device_id) "
                         "VALUES (%s, 'from_patient', 'reply', %s, 'patient', %s)",
                         (ev.patient_id, checkin.describe(data.get("answer"), data.get("question_id")), ev.device_id))
        publish(self.pool, {"type": "message", "patient_id": ev.patient_id})

    # ------------------------------------------------------------------ agent queue
    def _agent_runs_last_24h(self) -> int:
        with self.pool.connection() as conn:
            return conn.execute("SELECT count(*) FROM agent_runs WHERE started_at > now() - interval '24 hours' "
                                "AND trigger = ANY(%s)", (list(AgentThrottle.AUTOMATIC),)).fetchone()[0]

    def enqueue(self, task: AgentTask, new_alert: bool = False) -> int:
        """Queue agent work. Failures are logged, never raised: alerts must not depend on the agents."""
        rank = SEVERITY_RANK.get(clamp_severity((task.event or {}).get("severity", "medium")), 0) \
            if task.trigger == "alert" else 0
        outcome = self.throttle.admit(task.trigger, task.patient_id, rank, new_alert)
        if outcome != "queued" and new_alert:
            self._not_summarised(task.alert_id, f"the daily limit of {self.cfg.agent_daily_limit} automatic agent runs "
                                                "is reached" if outcome == "capped" else "the agents reviewed this "
                                                "patient moments ago")
        if outcome != "queued":
            AGENT_TASKS.labels(task.trigger, outcome).inc()
            if outcome == "capped" and time.monotonic() - self._capped_logged > 600:
                self._capped_logged = time.monotonic()
                log.warning("daily agent-run limit reached; alerts still page, agent summaries resume as runs age out",
                            extra={"limit": self.cfg.agent_daily_limit, "patient_id": task.patient_id})
            return 0
        try:
            if self._queue_url is None:
                self._queue_url = self.sqs.get_queue_url(QueueName=self.cfg.sqs_agent_queue)["QueueUrl"]
            self.sqs.send_message(QueueUrl=self._queue_url, MessageBody=task.to_json())
            AGENT_TASKS.labels(task.trigger, "queued").inc()
            return 1
        except Exception as exc:  # noqa: BLE001
            AGENT_TASKS.labels(task.trigger, "failed").inc()
            log.warning("agent task not queued", extra={"patient_id": task.patient_id, "error": str(exc)[:200]})
            if new_alert:
                self._not_summarised(task.alert_id, "the agent queue could not be reached")
            return 0

    def _not_summarised(self, alert_id: str | None, why: str) -> None:
        """A new alert whose agent run was not started: say so on the alert, instead of a summary that never comes."""
        if not alert_id:
            return
        try:
            self.store.attach_brief(alert_id, None, "skipped", None, "care-api")
            self.store.add_timeline(alert_id, "agent_brief", "system",
                                    f"No agent summary was started: {why}. Ask the agents for a review.")
            self.changed(alert_id)
        except Exception as exc:  # noqa: BLE001 - the alert itself is already open and paging
            log.warning("could not mark the alert as not summarised", extra={"alert_id": alert_id,
                                                                            "error": str(exc)[:200]})

    # ------------------------------------------------------------------ timers + outbox
    def _send(self, alert_row: dict, tier: int) -> list[dict]:
        records = self.notifier.send(alert_row, tier)
        for n in records:
            NOTIFIED.labels(n["channel"], n["status"]).inc()
            if n["status"] == "sent" and n.get("latency_ms") is not None:
                NOTIFY_LATENCY.labels(n["channel"], n["cause"]).observe(n["latency_ms"] / 1000)
        return records

    def tick(self) -> None:
        for row in self.store.run_due_escalations():
            ESCALATED.labels(str(row["tier"])).inc()
            self.changed(row["alert_id"])
        for row in self.store.dispatch_pending(self._send):
            self.changed(row["alert_id"])
        if time.monotonic() - self._returned_checked >= RETURNED_CHECK_S:
            self._returned_checked = time.monotonic()
            for row in self.store.resolve_returned(switched_off=not self.cfg.silent_device_alerts):
                AUTO_RESOLVED.inc()
                log.info("silent-device alert closed",
                         extra={"alert_id": str(row["alert_id"]), "patient_id": row["patient_id"]})
                self.changed(row["alert_id"])

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("escalation/dispatch tick failed")
            if self._wake.wait(self.cfg.tick_s):
                # One telemetry window often yields several alerting events (an on-device alert, then a critical
                # SpO2) tens of milliseconds apart. Settling briefly lets them all raise the alert first, so the
                # care team gets one page at the final severity instead of two back-to-back pages.
                stop.wait(self.cfg.dispatch_settle_s)
            self._wake.clear()

    def nudge(self) -> None:
        self._wake.set()


def _rank_dict(e: dict | None) -> tuple[int, int]:
    e = e or {}
    return (1 if e.get("requires_alert") else 0, SEVERITY_RANK.get(clamp_severity(e.get("severity", "medium")), 0))


def _rank(ev: ClinicalEvent) -> tuple[int, int]:
    return (1 if ev.requires_alert else 0, SEVERITY_RANK.get(clamp_severity(ev.severity), 0))
