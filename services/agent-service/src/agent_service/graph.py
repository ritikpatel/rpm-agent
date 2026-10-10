"""The clinical agent workflow (LangGraph).

    start ──┬── reply ──► handle_reply ───────────────────────────────────────────────┐
            └── otherwise ──► monitoring ──► risk ──► escalation ──► summary ──► persist

monitoring  what is happening, and is the signal trustworthy?            (reads vitals and events)
risk        how serious is it, given history and the risk models?        (reads risk scores and the record)
escalation  who must act and how fast? Its recommendations pass the deterministic policy gate, then are carried out
            through care-api (raise only, never lower)                   (reads alert state, searches protocols)
summary     cited SBAR handover for the care team (RAG over protocols and the patient's own record)

Every agent reads through care-api tools under its own name. If the model is unavailable or an agent fails, the run
degrades to deterministic rules and a templated summary: the care team still gets a usable result, labelled as such.
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
from typing import Annotated, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import ValidationError

from rpm_core.contracts import AgentTask, utcnow, uuid7
from rpm_core.rag import Hit

from . import offline
from .api import CareApi
from .llm import BedrockLLM, LLMUnavailable, Tool
from .policy import CHECKIN_QUESTIONS, SEVERITY_RANK, checkin_topic, gate
from .prompts import ESCALATION_SYSTEM, MONITORING_SYSTEM, RISK_SYSTEM, SUMMARY_SYSTEM
from .schemas import (
    BRIEF_SCHEMA,
    ESCALATION_SCHEMA,
    MONITORING_SCHEMA,
    RISK_SCHEMA,
    Brief,
    EscalationOut,
    MonitoringOut,
    RiskOut,
)

log = logging.getLogger(__name__)
DATA_BLOCKS = {"EVENT", "MONITORING", "RISK", "ESCALATION", "ACTIONS", "CURRENT", "TREND_6H", "RECENT_WINDOWS",
               "EVENTS_24H", "ACTIVE_ALERT", "RISK_MODELS", "RECORD", "AGENT_ASSESSMENT", "RECENT_AGENT_ACTIONS"}
_REF = re.compile(r"\[([A-Za-z0-9_\-]+(?:#\d+)?)\]")
# Clinical vocabulary per event type, so retrieval finds the right protocol even for terse device events.
EVENT_HINTS = {
    "SOS": "SOS fall emergency response patient does not respond", "FALL": "fall detected SOS head injury",
    "SPO2_LOW": "low oxygen saturation hypoxia verify measurement", "SPO2_CRITICAL": "low oxygen saturation hypoxia",
    "HR_SUSTAINED_HIGH": "fast heart rate tachycardia irregular rhythm", "HR_SUSTAINED_LOW": "slow heart rate",
    "NEWS2_HIGH": "NEWS2 aggregate emergency response", "NEWS2_MEDIUM": "NEWS2 urgent response",
    "NEWS2_RED_PARAM": "NEWS2 single parameter red score", "DEVICE_SILENT": "device silent no data",
    "EDGE_ALERT": "wearable alert verify", "DISCHARGED": "post-discharge follow-up monitoring readmission",
    "RISK_HIGH": "deterioration risk escalation review", "REVIEW": "post-discharge follow-up monitoring",
}
SUMMARY_TRIGGERS = {"alert", "discharge", "on_demand", "daily_review"}
REVIEW_TEXT = {"on_demand": "Clinician asked for a review of this patient",
               "daily_review": "Scheduled daily review", "discharge": "Patient discharged; home monitoring started"}
EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
SEARCH_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
    "query": {"type": "string", "description": "What to look for, in clinical terms"},
    "kind": {"type": "string", "enum": ["protocol", "discharge_summary"],
             "description": "protocol = escalation protocols; discharge_summary = this patient's own record"}}}


class RunState(TypedDict, total=False):
    run_id: str
    started_at: datetime
    task: dict
    event: dict
    trigger: str
    patient_id: str
    alert_id: str | None
    profile: dict
    monitoring: dict
    risk: dict
    escalation: dict
    assessment: dict
    assessment_by: str
    decision: dict
    brief: dict
    brief_status: str
    hits: Annotated[list, operator.add]
    actions: Annotated[list, operator.add]
    usage: Annotated[list, operator.add]
    trace: Annotated[list, operator.add]
    errors: Annotated[list, operator.add]


@dataclass
class Deps:
    api: CareApi
    llm: BedrockLLM | None
    model_fast: str = "au.anthropic.claude-haiku-4-5-20251001-v1:0"
    model_reasoning: str = "au.anthropic.claude-sonnet-4-6"


def classify(ev: dict) -> str:
    """Trigger for a bare clinical event (care-api applies the same mapping when it queues agent work)."""
    if ev["type"] == "CHECKIN_REPLY":
        return "reply"
    if ev["requires_alert"]:
        return "alert"
    if ev["type"] in ("NEWS2_RISING", "RISK_HIGH"):
        return "monitor"
    if ev["type"] == "DISCHARGED":
        return "discharge"
    return "record"


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


def _j(value) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def _dedupe(hits: list[Hit]) -> list[Hit]:
    seen, out = set(), []
    for h in hits:
        if h.chunk_id not in seen:
            seen.add(h.chunk_id)
            out.append(h)
    return out


class AgentGraph:
    def __init__(self, deps: Deps):
        self.d = deps
        g = StateGraph(RunState)
        for name in ("start", "monitoring", "risk", "escalation", "summary", "handle_reply", "persist"):
            g.add_node(name, self._timed(name, getattr(self, name)))
        g.set_entry_point("start")
        g.add_conditional_edges("start", lambda s: "handle_reply" if s["trigger"] == "reply" else "monitoring",
                                {"handle_reply": "handle_reply", "monitoring": "monitoring"})
        g.add_edge("monitoring", "risk")
        g.add_edge("risk", "escalation")
        g.add_conditional_edges("escalation", lambda s: "summary" if s["trigger"] in SUMMARY_TRIGGERS else "persist",
                                {"summary": "summary", "persist": "persist"})
        g.add_edge("summary", "persist")
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

    def run(self, task: AgentTask) -> RunState:
        now = utcnow()
        event = task.event or {
            "event_id": None, "patient_id": task.patient_id, "type": "REVIEW", "severity": "info",
            "requires_alert": False, "occurred_at": now.isoformat(), "evidence": {},
            "summary": REVIEW_TEXT.get(task.trigger, "Review requested")}
        state: RunState = {"run_id": str(uuid7()), "task": task.to_dict(), "event": event, "trigger": task.trigger,
                           "patient_id": task.patient_id, "alert_id": task.alert_id, "started_at": now,
                           "hits": [], "actions": [], "usage": [], "trace": [], "errors": []}
        return self.app.invoke(state)

    # ------------------------------------------------------------------ helpers
    def _agent(self, name: str, model: str, system: str, user: str, tools: list[Tool], schema: dict, cls):
        """Run one agent. Returns (validated output, usage). Raises LLMUnavailable / ValidationError."""
        if self.d.llm is None:
            raise LLMUnavailable("no model configured")
        res = self.d.llm.run(model=model, system=system, user=user, tools=tools, output_schema=schema)
        data = cls.model_validate(res.data).model_dump()
        usage = {"agent": name, "model": res.model, "input": res.input_tokens, "output": res.output_tokens,
                 "ms": res.latency_ms, "turns": res.turns, "tools": [c["tool"] for c in res.tool_calls]}
        return data, usage

    def _read(self, s: RunState, agent: str, section: str, description: str, fresh: bool = False) -> Tool:
        return Tool(f"get_{section}", description, EMPTY,
                    lambda _args: self.d.api.section(s["patient_id"], section, agent, fresh))

    def _search(self, s: RunState, agent: str, found: list[Hit]) -> Tool:
        def handler(args: dict) -> list[dict]:
            hits = self.d.api.search(agent, str(args.get("query", ""))[:400], s["patient_id"], args.get("kind"),
                                     k=5, with_actions=args.get("kind") != "discharge_summary")
            found.extend(hits)
            return [{"id": h.chunk_id, "title": h.title, "heading": h.heading, "text": h.text} for h in hits]
        return Tool("search_guidelines", "Search the clinical protocols, or this patient's discharge summary. "
                                         "Returns passages with ids you can cite.", SEARCH_SCHEMA, handler)

    def _event_block(self, s: RunState) -> str:
        ev, p = s["event"], s.get("profile") or {}
        keep = {k: ev.get(k) for k in ("type", "severity", "summary", "occurred_at", "evidence")}
        who = {k: p.get(k) for k in ("age", "sex", "primary_condition", "care_setting", "hypercapnic_resp_failure")}
        return f"[EVENT] {_j(keep)}\n[PATIENT] {_j(who)}"

    def _legacy_ctx(self, s: RunState) -> dict:
        """The context the deterministic fallback works from, read through the same audited API."""
        api, pid = self.d.api, s["patient_id"]
        vitals = api.section(pid, "vitals", "fallback")
        events = api.section(pid, "events", "fallback")
        return {"profile": s["profile"], "status": vitals["current"], "trend": vitals["trend_6h"],
                "recent": vitals["recent_windows"], "events": events["events_24h"], "alert": events["active_alert"],
                "recent_actions": events["recent_agent_actions"],
                "risk": api.section(pid, "risk", "fallback")["risk_models"]}

    # ------------------------------------------------------------------ nodes
    def start(self, s: RunState) -> dict:
        profile = self.d.api.section(s["patient_id"], "profile", "orchestrator")["profile"]
        return {"profile": profile}

    def monitoring(self, s: RunState) -> dict:
        tools = [self._read(s, "monitoring", "vitals", "Current vitals and NEWS2, the 6-hour trend in 30-minute "
                                                         "buckets, and the 12 most recent windows."),
                 self._read(s, "monitoring", "events", "Clinical events of the last 24 hours, the active alert, "
                                                         "recent agent actions and messages with the patient.")]
        if self.d.llm is None:
            return {}  # offline by configuration: the escalation node applies the deterministic rules
        try:
            data, usage = self._agent("monitoring", self.d.model_fast, MONITORING_SYSTEM,
                                      self._event_block(s) + "\n\nAssess the patient's current state.",
                                      tools, MONITORING_SCHEMA, MonitoringOut)
            return {"monitoring": data, "usage": [usage]}
        except (LLMUnavailable, ValidationError, Exception) as exc:  # noqa: BLE001
            return {"errors": [f"monitoring agent unavailable: {str(exc)[:200]}"]}

    def risk(self, s: RunState) -> dict:
        if not s.get("monitoring"):
            return {}
        tools = [self._read(s, "risk", "risk", "Latest score, band and top factors of each risk model, and whether "
                                               "the model is validated.", fresh=True),
                 self._read(s, "risk", "record", "Problem list, medications and recent lab results.")]
        user = (self._event_block(s) + f"\n[MONITORING] {_j(s['monitoring'])}\n\n"
                "Judge how serious this is for the care team.")
        try:
            data, usage = self._agent("risk", self.d.model_fast, RISK_SYSTEM, user, tools, RISK_SCHEMA, RiskOut)
            return {"risk": data, "usage": [usage]}
        except (LLMUnavailable, ValidationError, Exception) as exc:  # noqa: BLE001
            return {"errors": [f"risk agent unavailable: {str(exc)[:200]}"]}

    def escalation(self, s: RunState) -> dict:
        api, ev, found = self.d.api, s["event"], []
        out: dict = {}
        escalation = None
        if s.get("monitoring") and s.get("risk"):
            tools = [self._read(s, "escalation", "events", "The active alert (severity, tier, acknowledged or not), "
                                                           "recent agent actions and events of the last 24 hours."),
                     self._search(s, "escalation", found)]
            user = (self._event_block(s) + f"\n[MONITORING] {_j(s['monitoring'])}\n[RISK] {_j(s['risk'])}\n\n"
                    "Decide what should happen next.")
            try:
                escalation, usage = self._agent("escalation", self.d.model_reasoning, ESCALATION_SYSTEM, user, tools,
                                                ESCALATION_SCHEMA, EscalationOut)
                out["usage"] = [usage]
            except (LLMUnavailable, ValidationError, Exception) as exc:  # noqa: BLE001
                out["errors"] = [f"escalation agent unavailable: {str(exc)[:200]}"]
        if escalation is not None:
            m, r = s["monitoring"], s["risk"]
            assessment = {
                "signal_quality": m["signal_quality"], "concern_level": r["concern_level"],
                "clinical_picture": f"{m['clinical_picture']} {r['risk_summary']}".strip(),
                "key_findings": [*m["key_findings"], *r["key_findings"]][:8],
                "recommended_actions": escalation["recommended_actions"],
                "model_evidence_note": r["model_evidence_note"], "confidence": r["confidence"]}
            by = "agents"
        else:
            ctx = self._legacy_ctx(s)
            assessment, by = offline.assess(ctx, ev), "offline-rules"
        events = api.section(s["patient_id"], "events", "orchestrator")
        alert = events["active_alert"]
        alert_id = s.get("alert_id") or (str(alert["alert_id"]) if alert else None)
        rule_severity = ev["severity"] if ev["severity"] in SEVERITY_RANK else "medium"
        d = gate(assessment, care_setting=s["profile"]["care_setting"], rule_severity=rule_severity, alert=alert,
                 recent_actions=events["recent_agent_actions"], now=utcnow())
        if alert_id:
            api.timeline(alert_id, "risk", "agent_assessment",
                         f"Concern {assessment['concern_level']}, signal {assessment['signal_quality']}: "
                         f"{assessment['clinical_picture'][:400]}", {"assessment": assessment, "blocked": d.blocked,
                                                                     "by": by})
            if d.raise_severity_to:
                api.raise_severity(alert_id, "escalation", d.raise_severity_to, assessment["clinical_picture"][:200])
            if d.disagreement:
                api.timeline(alert_id, "escalation", "note", d.disagreement)
        out.update(escalation=escalation or {}, assessment=assessment, assessment_by=by, alert_id=alert_id,
                   decision={"allowed": d.allowed, "blocked": d.blocked, "raised_to": d.raise_severity_to,
                             "disagreement": d.disagreement},
                   actions=self._act(s, d.allowed, alert_id, events), hits=found)
        return out

    def _act(self, s: RunState, allowed: list[dict], alert_id: str | None, events: dict) -> list[dict]:
        api, ev, pid = self.d.api, s["event"], s["patient_id"]
        done = []
        for rec in allowed:
            action, detail, status = rec["action"], {}, "done"
            try:
                if action == "ESCALATE":
                    api.escalate(alert_id, "escalation", rec["reason"])
                elif action == "REQUEST_SPOT_CHECK":
                    detail = api.device(pid, "escalation", "SPOT_CHECK", {"metric": "spo2"})
                elif action in ("SEND_CHECKIN", "REQUEST_HOME_READINGS"):
                    topic = "readings" if action == "REQUEST_HOME_READINGS" else checkin_topic(
                        ev["type"], s["profile"].get("primary_condition"), {})
                    qid = f"{topic}-{s['run_id'][-8:]}"
                    detail = api.device(pid, "escalation", "CHECKIN", {
                        "question_id": qid, "text": CHECKIN_QUESTIONS[topic], "options": ["Yes", "No"]})
                    detail.update(question_id=qid, question=CHECKIN_QUESTIONS[topic])
                elif action in ("SET_PROFILE_ELEVATED", "SET_PROFILE_CRITICAL"):
                    profile = "critical" if action.endswith("CRITICAL") else "elevated"
                    detail = {**api.device(pid, "escalation", "SET_PROFILE", {"profile": profile}),
                              "profile": profile}
            except Exception as exc:  # noqa: BLE001 - an action failure must not abort the run
                status, detail = "failed", {"error": str(exc)[:200]}
            entry = {"action": action, "reason": rec["reason"], "status": status, "at": utcnow().isoformat(), **detail}
            done.append(entry)
            if alert_id:
                try:
                    api.timeline(alert_id, "escalation", "agent_action",
                                 f"{action.replace('_', ' ').lower()} ({status}): {rec['reason']}", entry)
                except Exception:  # noqa: BLE001
                    log.warning("could not record agent action on the alert timeline")
        return done

    def summary(self, s: RunState) -> dict:
        api, ev, a = self.d.api, s["event"], s["assessment"]
        cond = s["profile"].get("primary_condition") or ""
        query = " ".join([EVENT_HINTS.get(ev["type"], ev["type"].replace("_", " ").lower()), cond, ev["summary"],
                          *(f["finding"] for f in a.get("key_findings", [])[:3])])
        hits = list(s.get("hits", []))
        try:
            hits += api.search("summary", query, s["patient_id"], "protocol", k=6, with_actions=True)
            hits += api.search("summary", f"{cond} admission history discharge", s["patient_id"],
                               "discharge_summary", k=2)
        except Exception as exc:  # noqa: BLE001
            return {"brief": None, "brief_status": "failed", "errors": [f"retrieval failed: {str(exc)[:200]}"]}
        hits = _dedupe(hits)

        def templated(note: str | None = None) -> dict:
            ctx = self._legacy_ctx(s)
            b = offline.brief(ctx, ev, a, hits) | {"generated_by": "offline-rules", "citation_check": "template"}
            return {"brief": b, "brief_status": "offline", "hits": hits, **({"errors": [note]} if note else {})}

        if self.d.llm is None or s["assessment_by"] == "offline-rules":
            return templated()
        found: list[Hit] = []
        sources = "\n".join(f"[{h.chunk_id}] {h.title} — {h.heading}: {h.text}" for h in hits)
        user = (self._event_block(s) + f"\n[MONITORING] {_j(s['monitoring'])}\n[RISK] {_j(s['risk'])}\n"
                f"[ESCALATION] {_j(s['escalation'])}\n[ACTIONS] {_j(s.get('actions', []))}\n\nSOURCES:\n{sources}\n\n"
                "Write the SBAR summary.")
        tools = [self._search(s, "summary", found),
                 self._read(s, "summary", "record", "Problem list, medications and recent lab results.")]
        try:
            data, usage = self._agent("summary", self.d.model_reasoning, SUMMARY_SYSTEM, user, tools, BRIEF_SCHEMA,
                                      Brief)
        except (LLMUnavailable, ValidationError, Exception) as exc:  # noqa: BLE001
            return templated(f"summary agent unavailable: {str(exc)[:200]}")
        hits = _dedupe(hits + found)
        ok, problems = check_citations(data, hits)
        data["citation_check"] = "passed" if ok else "needs review: " + "; ".join(problems)
        data["generated_by"] = f"bedrock:{usage['model']}"
        return {"brief": data, "brief_status": "ready", "usage": [usage], "hits": hits}

    def handle_reply(self, s: RunState) -> dict:
        """The patient answered a check-in on the watch. 'Yes' to a symptom question raises the alert one level."""
        api, ev = self.d.api, s["event"]
        answer = str(ev.get("evidence", {}).get("answer", "")).strip().lower()
        qid = str(ev.get("evidence", {}).get("question_id") or "")
        alert = api.section(s["patient_id"], "events", "orchestrator")["active_alert"]
        alert_id = str(alert["alert_id"]) if alert else None
        symptom = answer == "yes" and not qid.startswith("readings")
        if alert_id:
            api.timeline(alert_id, "escalation", "patient_reply", f"Answered '{answer}' to {qid}", ev.get("evidence"))
            nxt = {"medium": "high", "high": "critical"}.get(alert["severity"])
            if symptom and nxt:
                api.raise_severity(alert_id, "escalation", nxt, "patient reports symptoms on check-in")
        elif symptom:  # symptoms with no active alert: open one through the same audited path
            alert_id = api.open_alert("escalation", {**ev, "severity": "medium", "requires_alert": True})
        return {"alert_id": alert_id, "actions": [{"action": "PATIENT_REPLY", "reason": f"answer={answer}",
                                                   "status": "done", "at": utcnow().isoformat()}]}

    def persist(self, s: RunState) -> dict:
        api, ev, alert_id = self.d.api, s["event"], s.get("alert_id")
        usage = s.get("usage", [])
        if alert_id and s.get("brief_status"):
            api.brief(alert_id, "summary", s.get("brief"), s["brief_status"], s.get("assessment"))
        errors = s.get("errors", [])
        status = "failed" if errors and not s.get("assessment") and s["trigger"] != "reply" else \
            "degraded" if errors else "succeeded"
        api.save_run({
            "run_id": s["run_id"], "task_id": s["task"].get("task_id"), "event_id": ev.get("event_id"),
            "patient_id": s["patient_id"], "alert_id": alert_id, "trigger": s["trigger"],
            "started_at": s["started_at"].isoformat(), "status": status,
            "provider": "bedrock" if usage else "offline",
            "model": ", ".join(sorted({u["model"] for u in usage})) or "offline-rules",
            "input_tokens": sum(u["input"] for u in usage) or None,
            "output_tokens": sum(u["output"] for u in usage) or None,
            "assessment": s.get("assessment"), "actions": s.get("actions") or [], "brief": s.get("brief"),
            "trace": {"nodes": s.get("trace", []), "usage": usage, "decision": s.get("decision"),
                      "agents": {k: s.get(k) for k in ("monitoring", "risk", "escalation") if s.get(k)},
                      "retrieved": [h.chunk_id for h in s.get("hits", [])]},
            "error": "; ".join(errors) or None})
        return {}
