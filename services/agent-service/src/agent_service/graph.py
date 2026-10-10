"""The clinical agent workflow (LangGraph).

    load_context ──┬── reply ──► handle_reply ───────────────────────────────┐
                   ├── record ───────────────────────────────────────────────┤
                   └── alert / monitor ──► assess ──► gate ──► act ──┬──► retrieve ──► brief ──► persist
                                                                      └── (monitor) ───────────► persist

assess  = monitoring + risk agent (Claude, structured output; offline rules on failure)
gate    = deterministic escalation policy (can raise, never lower; filters actions)
act     = device commands + alert escalation through audited interfaces
brief   = summarisation agent (RAG over protocols + the patient's own record, cited SBAR)
"""

from __future__ import annotations

import json
import logging
import operator
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph
from psycopg.types.json import Jsonb
from pydantic import ValidationError
from redis import Redis

from care_api.alerts import SEVERITY_RANK, AlertStore, clamp_severity
from rpm_core.devices import DeviceCommander
from rpm_core.live import publish
from rpm_core.contracts import ClinicalEvent, utcnow, uuid7
from rpm_core.rag import Hit, RagIndex

from . import offline
from .context import ContextBuilder, render
from .llm import ClaudeClient, LLMUnavailable
from .policy import CHECKIN_QUESTIONS, gate
from .prompts import ASSESSOR_SYSTEM, BRIEF_SYSTEM
from .schemas import ASSESSMENT_SCHEMA, BRIEF_SCHEMA, Assessment, Brief

log = logging.getLogger(__name__)
DATA_BLOCKS = {"EVENT", "PROFILE", "CURRENT", "TREND_6H", "RECENT_WINDOWS", "EVENTS_24H", "ACTIVE_ALERT",
               "RISK_MODELS", "RECORD", "AGENT_ASSESSMENT", "RECENT_AGENT_ACTIONS"}
_REF = re.compile(r"\[([A-Za-z0-9_\-]+(?:#\d+)?)\]")
# Clinical vocabulary per event type, so retrieval finds the right protocol even for terse device events.
EVENT_HINTS = {
    "SOS": "SOS fall emergency response patient does not respond", "FALL": "fall detected SOS head injury",
    "SPO2_LOW": "low oxygen saturation hypoxia verify measurement", "SPO2_CRITICAL": "low oxygen saturation hypoxia",
    "HR_SUSTAINED_HIGH": "fast heart rate tachycardia irregular rhythm", "HR_SUSTAINED_LOW": "slow heart rate",
    "NEWS2_HIGH": "NEWS2 aggregate emergency response", "NEWS2_MEDIUM": "NEWS2 urgent response",
    "NEWS2_RED_PARAM": "NEWS2 single parameter red score", "DEVICE_SILENT": "device silent no data",
    "EDGE_ALERT": "wearable alert verify",
}
ACTION_HEADINGS = ("action", "response", "immediate", "if the patient")


class RunState(TypedDict, total=False):
    run_id: str
    started_at: datetime
    event: dict
    trigger: str
    ctx: dict
    alert_id: str | None
    assessment: dict
    assessment_by: str
    decision: dict
    hits: list
    brief: dict
    brief_status: str
    brief_by: str
    actions: Annotated[list, operator.add]
    usage: Annotated[list, operator.add]
    trace: Annotated[list, operator.add]
    errors: Annotated[list, operator.add]


@dataclass
class Deps:
    pool: Any
    store: AlertStore
    commander: DeviceCommander
    llm: ClaudeClient | None
    rag: RagIndex
    builder: ContextBuilder
    redis: Redis
    effort_assess: str = "medium"
    effort_brief: str = "medium"
    alert_wait_s: float = 6.0


def classify(ev: dict) -> str:
    if ev["type"] == "CHECKIN_REPLY":
        return "reply"
    if ev["requires_alert"]:
        return "alert"
    if ev["type"] == "NEWS2_RISING":
        return "monitor"
    return "record"


def burst_lead(ev: dict, burst: list[dict]) -> dict:
    """Events persisted from one telemetry batch (say NEWS2_RISING + EDGE_ALERT + SPO2_CRITICAL) describe one clinical
    change. The agent runs once, on the most severe alerting event among them. The triggering event wins ties, so a
    run is never swapped for an equally severe sibling."""
    def key(e: dict) -> tuple[int, int]:
        return (1 if e["requires_alert"] else 0, SEVERITY_RANK.get(clamp_severity(e["severity"]), 0))
    best = ev
    for e in burst:
        if classify(e) in ("alert", "monitor") and key(e) > key(best):
            best = e
    return best


def check_citations(brief: dict, hits: list[Hit]) -> tuple[bool, list[str]]:
    valid = {h.chunk_id for h in hits}
    text = " ".join(str(brief.get(k, "")) for k in ("situation", "background", "assessment", "recommendation"))
    inline = set(_REF.findall(text))
    unknown = sorted({r for r in inline | {c["ref"] for c in brief.get("citations", [])}
                      if r not in valid and r not in DATA_BLOCKS})
    protocol_cited = any(r.startswith("P-") and r in valid for r in _REF.findall(brief.get("recommendation", "")))
    problems = [f"unknown reference {r}" for r in unknown]
    if not protocol_cited:
        problems.append("recommendation cites no protocol")
    return not problems, problems


class AgentGraph:
    def __init__(self, deps: Deps):
        self.d = deps
        g = StateGraph(RunState)
        for name in ("load_context", "assess", "gate", "act", "retrieve", "brief", "handle_reply", "persist"):
            g.add_node(name, self._timed(name, getattr(self, name)))
        g.set_entry_point("load_context")
        g.add_conditional_edges("load_context", lambda s: s["trigger"],
                                {"reply": "handle_reply", "record": "persist", "alert": "assess", "monitor": "assess"})
        g.add_edge("assess", "gate")
        g.add_edge("gate", "act")
        g.add_conditional_edges("act", lambda s: "retrieve" if s["trigger"] == "alert" else "persist",
                                {"retrieve": "retrieve", "persist": "persist"})
        g.add_edge("retrieve", "brief")
        g.add_edge("brief", "persist")
        g.add_edge("handle_reply", "persist")
        g.add_edge("persist", END)
        self.app = g.compile()

    @staticmethod
    def _timed(name: str, fn: Callable[[RunState], dict]) -> Callable[[RunState], dict]:
        def run(state: RunState) -> dict:
            t0 = time.perf_counter()
            out = fn(state) or {}
            out["trace"] = [*out.get("trace", []), {"node": name, "ms": round((time.perf_counter() - t0) * 1000)}]
            return out
        return run

    def run(self, event: ClinicalEvent) -> RunState:
        state: RunState = {"run_id": str(uuid7()), "event": event.to_dict(), "trigger": classify(event.to_dict()),
                           "actions": [], "usage": [], "trace": [], "errors": [], "started_at": utcnow()}
        return self.app.invoke(state)

    # ------------------------------------------------------------------ nodes
    def load_context(self, s: RunState) -> dict:
        ev = s["event"]
        if s["trigger"] == "record":  # informational events are only logged; no context, no model call
            return {"ctx": {}, "alert_id": None}
        ctx = self.d.builder.build(ev["patient_id"], score_risk=s["trigger"] in ("alert", "monitor"))
        if s["trigger"] == "alert" and ctx["alert"] is None:
            deadline = time.monotonic() + self.d.alert_wait_s  # alert-service consumes the parallel queue
            while ctx["alert"] is None and time.monotonic() < deadline:
                time.sleep(0.25)
                ctx["alert"] = self.d.store.active_for_patient(ev["patient_id"])
        return {"ctx": ctx, "alert_id": str(ctx["alert"]["alert_id"]) if ctx["alert"] else None}

    def _llm_json(self, system: str, user: str, schema: dict, effort: str, model_cls) -> tuple[dict, str, dict]:
        res = self.d.llm.complete_json(system=system, user=user, schema=schema, effort=effort)
        data = model_cls.model_validate(res.data).model_dump()
        usage = {"model": res.model, "input": res.input_tokens, "output": res.output_tokens,
                 "cache_read": res.cache_read_tokens, "ms": res.latency_ms}
        return data, f"{res.provider}:{res.model}", usage

    def assess(self, s: RunState) -> dict:
        ev, ctx = s["event"], s["ctx"]
        if self.d.llm is not None:
            try:
                user = render(ctx, ev) + "\n\nAssess this event for the care team."
                data, by, usage = self._llm_json(ASSESSOR_SYSTEM, user, ASSESSMENT_SCHEMA, self.d.effort_assess,
                                                 Assessment)
                return {"assessment": data, "assessment_by": by, "usage": [{"node": "assess", **usage}]}
            except (LLMUnavailable, ValidationError) as exc:
                err = f"assess degraded to offline rules: {str(exc)[:200]}"
                return {"assessment": offline.assess(ctx, ev), "assessment_by": "offline-rules", "errors": [err]}
        return {"assessment": offline.assess(ctx, ev), "assessment_by": "offline-rules"}

    def gate(self, s: RunState) -> dict:
        ev, ctx, a = s["event"], s["ctx"], s["assessment"]
        alert = ctx["alert"] if s.get("alert_id") else None
        d = gate(a, care_setting=ctx["profile"]["care_setting"], rule_severity=clamp_severity(ev["severity"]),
                 alert=alert, recent_actions=ctx["recent_actions"], now=utcnow())
        if s.get("alert_id"):
            self.d.store.add_timeline(s["alert_id"], "agent_assessment", f"agent:assessor ({s['assessment_by']})",
                                      f"Concern {a['concern_level']}, signal {a['signal_quality']}: "
                                      f"{a['clinical_picture'][:400]}", {"assessment": a, "blocked": d.blocked})
            if d.raise_severity_to and self.d.store.raise_severity(
                    s["alert_id"], d.raise_severity_to, "agent:escalation", a["clinical_picture"][:200]):
                publish(self.d.redis, {"type": "alert_changed", "alert_id": s["alert_id"]})
            if d.disagreement:
                self.d.store.add_timeline(s["alert_id"], "note", "agent:assessor", d.disagreement)
        return {"decision": {"allowed": d.allowed, "blocked": d.blocked, "raised_to": d.raise_severity_to,
                             "disagreement": d.disagreement}}

    def act(self, s: RunState) -> dict:
        ctx, ev = s["ctx"], s["event"]
        device = (ctx["status"] or {}).get("device_id")
        done = []
        for rec in s["decision"]["allowed"]:
            action, detail, status = rec["action"], {}, "done"
            try:
                if action == "ESCALATE":
                    self.d.store.escalate(s["alert_id"], "agent:escalation", rec["reason"])
                elif not device:
                    status, detail = "failed", {"error": "no active device"}
                elif action == "REQUEST_SPOT_CHECK":
                    cmd = self.d.commander.send(device, "SPOT_CHECK", {"metric": "spo2"}, "agent:monitor")
                    detail = {"cmd_id": str(cmd.cmd_id)}
                elif action in ("SEND_CHECKIN", "REQUEST_HOME_READINGS"):
                    topic = "readings" if action == "REQUEST_HOME_READINGS" else offline.checkin_for(ctx, ev)
                    qid = f"{topic}-{s['run_id'][-8:]}"
                    args = {"question_id": qid, "text": CHECKIN_QUESTIONS[topic], "options": ["Yes", "No"]}
                    cmd = self.d.commander.send(device, "CHECKIN", args, "agent:engagement")
                    detail = {"cmd_id": str(cmd.cmd_id), "question_id": qid, "question": CHECKIN_QUESTIONS[topic]}
                elif action in ("SET_PROFILE_ELEVATED", "SET_PROFILE_CRITICAL"):
                    profile = "critical" if action.endswith("CRITICAL") else "elevated"
                    cmd = self.d.commander.send(device, "SET_PROFILE", {"profile": profile}, "agent:monitor")
                    detail = {"cmd_id": str(cmd.cmd_id), "profile": profile}
            except Exception as exc:  # noqa: BLE001 - an action failure must not abort the run
                status, detail = "failed", {"error": str(exc)[:200]}
            entry = {"action": action, "reason": rec["reason"], "status": status, "at": utcnow().isoformat(), **detail}
            done.append(entry)
            if s.get("alert_id"):
                self.d.store.add_timeline(s["alert_id"], "agent_action", "agent:monitor",
                                          f"{action.replace('_', ' ').lower()} ({status}): {rec['reason']}", entry)
        return {"actions": done}

    def retrieve(self, s: RunState) -> dict:
        ev, ctx, a = s["event"], s["ctx"], s["assessment"]
        cond = ctx["profile"].get("primary_condition") or ""
        query = " ".join([EVENT_HINTS.get(ev["type"], ev["type"].replace("_", " ").lower()), cond, ev["summary"],
                          *(f["finding"] for f in a.get("key_findings", [])[:3])])
        hits = self.d.rag.search(query, ev["patient_id"], k=6, kind="protocol")
        if hits:  # make sure the best-matching protocol's action section is in the sources
            have = {h.chunk_id for h in hits}
            hits += [c for c in self.d.rag.doc_chunks(hits[0].doc_id)
                     if c.heading.lower().startswith(ACTION_HEADINGS) and c.chunk_id not in have]
        hits += self.d.rag.search(f"{cond} admission history discharge", ev["patient_id"], k=2,
                                  kind="discharge_summary")
        return {"hits": hits}

    def brief(self, s: RunState) -> dict:
        ev, ctx, a, hits = s["event"], s["ctx"], s["assessment"], s["hits"]
        if self.d.llm is not None and s["assessment_by"] != "offline-rules":
            sources = "\n".join(f"[{h.chunk_id}] {h.title} — {h.heading}: {h.text}" for h in hits)
            user = (render(ctx, ev) + f"\n[AGENT_ASSESSMENT] {json.dumps(a)}\n\nSOURCES:\n{sources}\n\n"
                    "Write the SBAR brief for this alert.")
            try:
                data, by, usage = self._llm_json(BRIEF_SYSTEM, user, BRIEF_SCHEMA, self.d.effort_brief, Brief)
                ok, problems = check_citations(data, hits)
                data["citation_check"] = "passed" if ok else "needs review: " + "; ".join(problems)
                data["generated_by"] = by
                return {"brief": data, "brief_status": "ready", "brief_by": by, "usage": [{"node": "brief", **usage}]}
            except (LLMUnavailable, ValidationError) as exc:
                err = f"brief degraded to offline template: {str(exc)[:200]}"
                b = offline.brief(ctx, ev, a, hits) | {"generated_by": "offline-rules", "citation_check": "template"}
                return {"brief": b, "brief_status": "offline", "brief_by": "offline-rules", "errors": [err]}
        b = offline.brief(ctx, ev, a, hits) | {"generated_by": "offline-rules", "citation_check": "template"}
        return {"brief": b, "brief_status": "offline", "brief_by": "offline-rules"}

    def handle_reply(self, s: RunState) -> dict:
        """Patient answered a check-in on the watch. 'Yes' to a symptom question raises the alert one level."""
        ev = s["event"]
        answer = str(ev.get("evidence", {}).get("answer", "")).strip().lower()
        qid = str(ev.get("evidence", {}).get("question_id") or "")
        alert_id = s.get("alert_id")
        symptom = answer == "yes" and not qid.startswith("readings")
        if alert_id:
            self.d.store.add_timeline(alert_id, "patient_reply", "patient", f"Answered '{answer}' to {qid}",
                                      ev.get("evidence"))
            if symptom:
                cur = s["ctx"]["alert"]["severity"]
                nxt = {"medium": "high", "high": "critical"}.get(cur)
                if nxt:
                    self.d.store.raise_severity(alert_id, nxt, "agent:engagement",
                                                "patient reports symptoms on check-in")
        elif symptom:  # symptoms with no active alert: open one through the same audited path
            self.d.store.ingest_event(ClinicalEvent.model_validate({**ev, "severity": "medium",
                                                                    "requires_alert": True}))
        return {"actions": [{"action": "PATIENT_REPLY", "reason": f"answer={answer}", "status": "done",
                             "at": utcnow().isoformat()}]}

    def persist(self, s: RunState) -> dict:
        ev = s["event"]
        alert_id = s.get("alert_id")
        if s.get("brief"):
            if not alert_id:
                active = self.d.store.active_for_patient(ev["patient_id"])
                alert_id = str(active["alert_id"]) if active else None
            if alert_id:
                self.d.store.attach_brief(alert_id, s["brief"], s["brief_status"], s.get("assessment"),
                                          f"agent:summariser ({s['brief_by']})")
        usage = s.get("usage", [])
        status = "failed" if s.get("errors") and not s.get("assessment") and s["trigger"] != "record" else \
            "degraded" if s.get("errors") else "succeeded" if s["trigger"] != "record" else "skipped"
        with self.d.pool.connection() as conn:
            conn.execute(
                "INSERT INTO agent_runs (run_id, event_id, patient_id, alert_id, trigger, started_at, finished_at, "
                "status, provider, model, input_tokens, output_tokens, cache_read_tokens, assessment, actions, brief, "
                "trace, error) VALUES (%s,%s,%s,%s,%s,%s,now(),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (s["run_id"], ev["event_id"], ev["patient_id"], alert_id, s["trigger"], s["started_at"], status,
                 (s.get("assessment_by") or "none").split(":")[0],
                 usage[0]["model"] if usage else s.get("assessment_by"),
                 sum(u["input"] for u in usage) or None, sum(u["output"] for u in usage) or None,
                 sum(u["cache_read"] for u in usage) or None,
                 Jsonb(s.get("assessment")) if s.get("assessment") else None,
                 Jsonb(s.get("actions") or []), Jsonb(s.get("brief")) if s.get("brief") else None,
                 Jsonb({"nodes": s.get("trace", []), "usage": usage, "decision": s.get("decision"),
                        "retrieved": [h.chunk_id for h in s.get("hits", [])]}),
                 "; ".join(s.get("errors", [])) or None))
        publish(self.d.redis, {"type": "agent_run", "run_id": s["run_id"], "patient_id": ev["patient_id"],
                               "alert_id": alert_id, "status": status, "trigger": s["trigger"]})
        if alert_id:
            publish(self.d.redis, {"type": "alert_changed", "alert_id": alert_id})
        return {"alert_id": alert_id}
