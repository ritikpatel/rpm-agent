"""Alert lifecycle on Postgres: open/attach, severity escalation, acknowledgement, resolution, timed escalation and a
notification outbox. Every state change and its timeline entry commit in one transaction."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from rpm_contracts import ClinicalEvent, utcnow, uuid7

from .policy import SEVERITY_RANK, EscalationPolicy, clamp_severity

log = logging.getLogger(__name__)

ACTIVE = ("open", "acknowledged", "escalated")
EVENT_LABELS = {
    "NEWS2_HIGH": "NEWS2 high", "NEWS2_MEDIUM": "NEWS2 medium", "NEWS2_RED_PARAM": "NEWS2 single parameter",
    "HR_SUSTAINED_HIGH": "Sustained tachycardia", "HR_SUSTAINED_LOW": "Sustained bradycardia",
    "SPO2_LOW": "Low SpO2", "SPO2_CRITICAL": "Critical SpO2", "SOS": "SOS pressed", "FALL": "Fall detected",
    "EDGE_ALERT": "On-device alert", "DEVICE_SILENT": "Monitor silent", "AGENT_ESCALATION": "Agent escalation",
}


@dataclass(frozen=True)
class IngestResult:
    alert_id: UUID
    created: bool
    severity_raised: bool


def _cause(actor: str) -> str:
    """Why a page is going out, from who asked for it: a sensor event, the agent, or a clinician."""
    return "event" if actor == "system" else "agent" if actor.startswith("agent") else "manual"


def _title(event_type: str, summary: str) -> str:
    return f"{EVENT_LABELS.get(event_type, event_type)} — {summary}"[:240]


class AlertStore:
    def __init__(self, pool: ConnectionPool, policy: EscalationPolicy, consumer: str = "alert-service"):
        self.pool = pool
        self.policy = policy
        self.consumer = consumer

    # ------------------------------------------------------------------ helpers
    # A page already pending keeps its first cause: latency is measured from the earliest reason to page.
    _CAUSE = ("notify_cause = CASE WHEN notify_pending_tier IS NULL THEN %(cause)s ELSE COALESCE(notify_cause, "
              "%(cause)s) END, notify_cause_at = CASE WHEN notify_pending_tier IS NULL THEN %(cause_at)s "
              "ELSE LEAST(notify_cause_at, %(cause_at)s) END")

    @staticmethod
    def _timeline(conn: psycopg.Connection, alert_id, kind: str, actor: str, summary: str,
                  details: dict | None = None) -> None:
        conn.execute("INSERT INTO alert_timeline (alert_id, kind, actor, summary, details) VALUES (%s,%s,%s,%s,%s)",
                      (alert_id, kind, actor, summary, Jsonb(details) if details else None))

    def _escalate_to(self, cur, row: dict, severity: str, actor: str, reason: str,
                     cause_at: datetime | None = None) -> bool:
        """Raise severity (never lower). Re-pages even if the alert had been acknowledged."""
        if SEVERITY_RANK[severity] <= SEVERITY_RANK[row["severity"]]:
            return False
        level = max(row["tier"], self.policy.initial(severity))
        cur.execute(
            "UPDATE alerts SET severity=%(severity)s, tier=%(level)s, status=%(status)s, "
            "notify_pending_tier=%(level)s, next_escalation_at=%(next)s, updated_at=now(), " + self._CAUSE +
            " WHERE alert_id=%(alert_id)s",
            # An alert already paged past its first tier stays "escalated": dropping back to "open" would make the
            # notifier treat the re-page as a first page and send the patient a second reassurance.
            {"severity": severity, "level": level, "next": utcnow() + self.policy.timeout(level),
             "status": "escalated" if level > row["tier"] or row["status"] == "escalated" else "open",
             "cause": _cause(actor), "cause_at": cause_at or utcnow(), "alert_id": row["alert_id"]})
        self._timeline(cur, row["alert_id"], "severity_raised", actor,
                       f"Severity {row['severity']} → {severity}: {reason}", {"tier": level})
        return True

    # ------------------------------------------------------------------ intake
    def ingest_event(self, ev: ClinicalEvent) -> IngestResult | None:
        """Open a new alert or attach to the patient's active one. Returns None if the event was already consumed."""
        for attempt in range(2):
            try:
                with self.pool.connection() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                    if not cur.execute(
                        "INSERT INTO consumed_events (consumer, event_id) VALUES (%s, %s) ON CONFLICT DO NOTHING "
                        "RETURNING 1", (self.consumer, ev.event_id)).fetchone():
                        return None
                    return self._ingest(cur, ev)
            except psycopg.errors.UniqueViolation:
                if attempt:  # concurrent open for the same patient: retry once, which attaches instead
                    raise
        raise AssertionError("unreachable")

    def _ingest(self, cur, ev: ClinicalEvent) -> IngestResult:
        severity = clamp_severity(ev.severity)
        row = cur.execute(
            "SELECT * FROM alerts WHERE patient_id=%s AND status = ANY(%s) FOR UPDATE", (ev.patient_id, list(ACTIVE))
        ).fetchone()
        details = {"event_id": str(ev.event_id), "type": ev.type, "severity": ev.severity, "summary": ev.summary,
                   "occurred_at": ev.occurred_at.isoformat(), "trace": ev.trace}
        if row is None:
            level = self.policy.initial(severity)
            alert_id = uuid7()
            cur.execute(
                "INSERT INTO alerts (alert_id, patient_id, status, severity, tier, title, source_event_id, "
                "event_types, first_event_at, next_escalation_at, notify_pending_tier, notify_cause, notify_cause_at) "
                "VALUES (%s,%s,'open',%s,%s,%s,%s,%s,%s,%s,%s,'event',%s)",
                (alert_id, ev.patient_id, severity, level, _title(ev.type, ev.summary), ev.event_id, [ev.type],
                 ev.occurred_at, utcnow() + self.policy.timeout(level), level, ev.occurred_at))
            self._timeline(cur, alert_id, "opened", "system",
                           f"{EVENT_LABELS.get(ev.type, ev.type)} ({severity}): {ev.summary}", details)
            return IngestResult(alert_id, True, False)

        cur.execute(
            "UPDATE alerts SET event_count = event_count + 1, updated_at = now(), "
            "event_types = (SELECT array_agg(DISTINCT t) FROM unnest(event_types || %s::text) t) WHERE alert_id=%s",
            (ev.type, row["alert_id"]))
        self._timeline(cur, row["alert_id"], "event_attached", "system", f"{ev.type}: {ev.summary}", details)
        raised = self._escalate_to(cur, row, severity, "system", f"{ev.type} ({ev.summary})", ev.occurred_at)
        return IngestResult(row["alert_id"], False, raised)

    # ------------------------------------------------------------------ clinician / agent actions
    def acknowledge(self, alert_id, user: str, note: str | None = None) -> dict | None:
        with self.pool.connection() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "UPDATE alerts SET status='acknowledged', acknowledged_at=now(), acknowledged_by=%s, "
                "next_escalation_at=NULL, notify_pending_tier=NULL, updated_at=now() "
                "WHERE alert_id=%s AND status IN ('open','escalated') RETURNING *", (user, alert_id)).fetchone()
            if row:
                secs = (row["acknowledged_at"] - row["opened_at"]).total_seconds()
                self._timeline(cur, alert_id, "acknowledged", f"user:{user}",
                               f"Acknowledged by {user} after {secs:.0f}s" + (f": {note}" if note else ""),
                               {"time_to_ack_s": round(secs, 1)})
            return row

    def resolve(self, alert_id, user: str, resolution: str) -> dict | None:
        with self.pool.connection() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "UPDATE alerts SET status='resolved', resolved_at=now(), resolved_by=%s, resolution=%s, "
                "next_escalation_at=NULL, notify_pending_tier=NULL, updated_at=now() "
                "WHERE alert_id=%s AND status = ANY(%s) RETURNING *", (user, resolution, alert_id, list(ACTIVE))
            ).fetchone()
            if row:
                self._timeline(cur, alert_id, "resolved", f"user:{user}", f"Resolved by {user}: {resolution}")
            return row

    def escalate(self, alert_id, actor: str, reason: str) -> dict | None:
        """Manual or agent-requested escalation to the next tier."""
        with self.pool.connection() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute("SELECT * FROM alerts WHERE alert_id=%s AND status = ANY(%s) FOR UPDATE",
                               (alert_id, list(ACTIVE))).fetchone()
            if not row:
                return None
            nxt = self.policy.next_level(row["tier"])
            if nxt is None:
                self._timeline(cur, alert_id, "note", actor, f"Escalation requested at top tier: {reason}")
                return row
            row = cur.execute(
                "UPDATE alerts SET tier=%(nxt)s, status='escalated', notify_pending_tier=%(nxt)s, "
                "next_escalation_at=%(next)s, updated_at=now(), " + self._CAUSE +
                " WHERE alert_id=%(alert_id)s RETURNING *",
                {"nxt": nxt, "next": utcnow() + self.policy.timeout(nxt), "cause": _cause(actor),
                 "cause_at": utcnow(), "alert_id": alert_id}).fetchone()
            self._timeline(cur, alert_id, "escalated", actor,
                           f"Escalated to {self.policy.tier(nxt).name}: {reason}", {"tier": nxt})
            return row

    def raise_severity(self, alert_id, severity: str, actor: str, reason: str) -> bool:
        with self.pool.connection() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute("SELECT * FROM alerts WHERE alert_id=%s AND status = ANY(%s) FOR UPDATE",
                               (alert_id, list(ACTIVE))).fetchone()
            return bool(row) and self._escalate_to(cur, row, clamp_severity(severity), actor, reason)

    def add_timeline(self, alert_id, kind: str, actor: str, summary: str, details: dict | None = None) -> None:
        with self.pool.connection() as conn:
            self._timeline(conn, alert_id, kind, actor, summary, details)

    def attach_brief(self, alert_id, brief: dict | None, status: str, assessment: dict | None, actor: str) -> None:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("UPDATE alerts SET brief=%s, brief_status=%s, agent_assessment=%s, updated_at=now() "
                         "WHERE alert_id=%s", (Jsonb(brief) if brief else None, status,
                                               Jsonb(assessment) if assessment else None, alert_id))
            if brief:
                self._timeline(conn, alert_id, "agent_brief", actor, brief.get("headline", "Clinical brief ready"))

    # ------------------------------------------------------------------ timers + outbox
    def run_due_escalations(self, now: datetime | None = None) -> list[dict]:
        """Advance unacknowledged alerts whose timer expired (SKIP LOCKED: safe with several replicas)."""
        changed = []
        with self.pool.connection() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                "SELECT * FROM alerts WHERE status IN ('open','escalated') AND next_escalation_at <= %s "
                "ORDER BY next_escalation_at LIMIT 25 FOR UPDATE SKIP LOCKED", (now or utcnow(),)).fetchall()
            for row in rows:
                nxt = self.policy.next_level(row["tier"])
                waited = self.policy.timeout(row["tier"]).total_seconds()
                if nxt is not None:
                    row = cur.execute(
                        "UPDATE alerts SET tier=%(nxt)s, status='escalated', notify_pending_tier=%(nxt)s, "
                        "next_escalation_at=%(next)s, updated_at=now(), " + self._CAUSE +
                        " WHERE alert_id=%(alert_id)s RETURNING *",
                        {"nxt": nxt, "next": utcnow() + self.policy.timeout(nxt), "cause": "timer",
                         "cause_at": row["next_escalation_at"], "alert_id": row["alert_id"]}).fetchone()
                    self._timeline(cur, row["alert_id"], "escalated", "system",
                                   f"Not acknowledged within {waited:.0f}s → {self.policy.tier(nxt).name}",
                                   {"tier": nxt})
                elif row["reminders_sent"] < self.policy.max_reminders:
                    row = cur.execute(
                        "UPDATE alerts SET reminders_sent = reminders_sent + 1, notify_pending_tier=tier, "
                        "next_escalation_at=%(next)s, updated_at=now(), " + self._CAUSE +
                        " WHERE alert_id=%(alert_id)s RETURNING *",
                        {"next": utcnow() + self.policy.timeout(row["tier"]), "cause": "timer",
                         "cause_at": row["next_escalation_at"], "alert_id": row["alert_id"]}).fetchone()
                    self._timeline(cur, row["alert_id"], "escalated", "system",
                                   f"Still unacknowledged — reminder {row['reminders_sent']} to "
                                   f"{self.policy.tier(row['tier']).name}")
                else:
                    row = cur.execute("UPDATE alerts SET next_escalation_at=NULL WHERE alert_id=%s RETURNING *",
                                       (row["alert_id"],)).fetchone()
                    self._timeline(cur, row["alert_id"], "note", "system",
                                   "Reminder limit reached; alert remains open on the console")
                changed.append(row)
        return changed

    def dispatch_pending(self, send) -> list[dict]:
        """Outbox: for each alert with a pending tier, call send(alert_row, tier) -> [notification dicts] while the
        row is locked, then record the notifications and clear the flag in the same transaction."""
        done = []
        with self.pool.connection() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                "SELECT a.*, p.display_name, p.care_setting, p.primary_condition, p.care_team, s.device_id "
                "FROM alerts a JOIN patients p USING (patient_id) LEFT JOIN patient_status s USING (patient_id) "
                "WHERE a.notify_pending_tier IS NOT NULL ORDER BY a.updated_at LIMIT 25 "
                "FOR UPDATE OF a SKIP LOCKED").fetchall()
            for row in rows:
                tier = row["notify_pending_tier"]
                records = send(row, tier)
                for n in records:
                    cur.execute(
                        "INSERT INTO notifications (notification_id, alert_id, tier, channel, recipient, status, "
                        "latency_ms, provider_ref, error, payload, cause) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (uuid7(), row["alert_id"], tier, n["channel"], n["recipient"], n["status"],
                         n.get("latency_ms"), n.get("provider_ref"), n.get("error"), Jsonb(n.get("payload") or {}),
                         n.get("cause")))
                sent = [f"{n['channel']}→{n['recipient']}" for n in records if n["status"] == "sent"]
                failed = [n["channel"] for n in records if n["status"] == "failed"]
                self._timeline(cur, row["alert_id"], "notified", "system",
                               f"Tier {tier} ({self.policy.tier(tier).name}) notified: {', '.join(sent) or 'none'}"
                               + (f"; failed: {', '.join(failed)}" if failed else ""),
                               {"tier": tier, "notifications": records})
                cur.execute("UPDATE alerts SET notify_pending_tier=NULL, notify_cause=NULL, notify_cause_at=NULL "
                            "WHERE alert_id=%s AND notify_pending_tier=%s", (row["alert_id"], tier))
                done.append(row)
        return done

    # ------------------------------------------------------------------ reads
    def get(self, alert_id) -> dict | None:
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            alert = cur.execute(
                "SELECT a.*, p.display_name, p.care_setting, p.primary_condition, p.age, p.sex "
                "FROM alerts a JOIN patients p USING (patient_id) WHERE alert_id=%s", (alert_id,)).fetchone()
            if not alert:
                return None
            alert["timeline"] = cur.execute(
                "SELECT at, kind, actor, summary, details FROM alert_timeline WHERE alert_id=%s ORDER BY at, id",
                (alert_id,)).fetchall()
            alert["notifications"] = cur.execute(
                "SELECT tier, channel, recipient, status, sent_at, latency_ms, cause, error FROM notifications "
                "WHERE alert_id=%s ORDER BY sent_at", (alert_id,)).fetchall()
            return alert

    def list(self, statuses: tuple[str, ...] = ACTIVE, limit: int = 100) -> list[dict]:
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                "SELECT a.alert_id, a.patient_id, a.status, a.severity, a.tier, a.title, a.event_types, a.event_count,"
                " a.opened_at, a.updated_at, a.acknowledged_at, a.acknowledged_by, a.resolved_at, a.brief_status, "
                " a.brief->>'headline' AS brief_headline, a.next_escalation_at, p.display_name, p.care_setting, "
                " p.primary_condition FROM alerts a JOIN patients p USING (patient_id) WHERE a.status = ANY(%s) "
                "ORDER BY CASE a.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, a.opened_at DESC "
                "LIMIT %s", (list(statuses), limit)).fetchall()

    def active_for_patient(self, patient_id: str) -> dict | None:
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            return cur.execute("SELECT * FROM alerts WHERE patient_id=%s AND status = ANY(%s)",
                               (patient_id, list(ACTIVE))).fetchone()
