"""Service-to-service endpoints (never exposed through the ingress).

- the stream processor hands over clinical events;
- the agents use the `/internal/agent/*` endpoints as their tools: every read of patient data and every action an
  agent takes goes through here, is checked, and is written to the audit log with the agent's name.

Callers authenticate with the shared service token. The agents cannot lower an alert's severity or close an alert:
those operations do not exist on this surface."""

from __future__ import annotations

import hmac
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from rpm_core.contracts import ClinicalEvent, utcnow, uuid7
from rpm_core.live import publish

from .context import SECTIONS

router = APIRouter(prefix="/internal")
STATE: dict = {}  # filled by main.lifespan: pool, engine, ctx, rag, commander, settings


def service(x_service_token: Annotated[str | None, Header()] = None,
            x_service_name: Annotated[str | None, Header()] = None,
            x_agent_name: Annotated[str | None, Header()] = None) -> str:
    """Returns the acting principal for the audit log, e.g. `agent:risk` or `service:stream-processor`."""
    if not x_service_token or not hmac.compare_digest(x_service_token, STATE["settings"].service_token):
        raise HTTPException(401, "service token required")
    return f"agent:{x_agent_name}" if x_agent_name else f"service:{x_service_name or 'unknown'}"


Caller = Annotated[str, Depends(service)]


def audit(actor: str, action: str, resource_type: str, resource_id: str | None, patient_id: str | None,
          details: dict | None = None) -> None:
    with STATE["pool"].connection() as conn:
        conn.execute(
            "INSERT INTO audit_log (actor, actor_role, action, resource_type, resource_id, patient_id, details) "
            "VALUES (%s, 'service', %s, %s, %s, %s, %s)",
            (actor, action, resource_type, resource_id, patient_id, Jsonb(details or {})))


# ------------------------------------------------------------------ stream processor -> alerts
class EventsIn(BaseModel):
    events: list[ClinicalEvent] = Field(max_length=2000)


@router.post("/events")
def events(body: EventsIn, caller: Caller) -> dict:
    return STATE["engine"].ingest(body.events)


@router.get("/cohort")
def cohort(caller: Caller) -> list[dict]:
    """Who is monitored, where, and on what kind of device (no clinical data). The simulator follows this."""
    with STATE["pool"].connection() as conn:
        rows = conn.execute(
            "SELECT p.patient_id, p.care_setting, d.device_id, d.device_kind, p.discharge_at FROM patients p "
            "LEFT JOIN device_assignments d ON d.patient_id = p.patient_id AND d.active_to IS NULL "
            "WHERE p.monitoring_active ORDER BY p.patient_id").fetchall()
    return [{"patient_id": r[0], "care_setting": r[1], "device_id": r[2], "device_kind": r[3],
             "discharge_at": r[4].isoformat() if r[4] else None} for r in rows]


# ------------------------------------------------------------------ agent tools: read
@router.get("/agent/patients/{patient_id}/{section}")
def read_section(patient_id: str, section: str, caller: Caller, fresh: bool = False) -> dict:
    if section not in SECTIONS:
        raise HTTPException(404, "unknown section")
    if STATE["ctx"].profile(patient_id) is None:
        raise HTTPException(404, "patient not found")
    audit(caller, "read", {"vitals": "Observation", "events": "Flag", "risk": "RiskAssessment", "record": "Patient",
                           "profile": "Patient"}[section], None, patient_id, {"section": section})
    return STATE["ctx"].section(patient_id, section, fresh)


class SearchIn(BaseModel):
    query: str = Field(min_length=2, max_length=600)
    patient_id: str | None = None
    kind: Literal["protocol", "discharge_summary"] | None = None
    k: int = Field(default=6, ge=1, le=12)
    with_actions: bool = False   # also return the action section of the best-matching protocol


@router.post("/agent/guidelines/search")
def search(body: SearchIn, caller: Caller) -> list[dict]:
    rag = STATE["rag"]
    hits = rag.search(body.query, body.patient_id, k=body.k, kind=body.kind)
    if body.with_actions and hits:
        have = {h.chunk_id for h in hits}
        hits += [c for c in rag.doc_chunks(hits[0].doc_id)
                 if c.heading.lower().startswith(("action", "response", "immediate", "if the patient"))
                 and c.chunk_id not in have]
    if body.patient_id:
        audit(caller, "read", "DocumentReference", None, body.patient_id, {"query": body.query[:200]})
    return [{"chunk_id": h.chunk_id, "doc_id": h.doc_id, "title": h.title, "heading": h.heading, "text": h.text,
             "kind": h.kind} for h in hits]


# ------------------------------------------------------------------ agent tools: act on an alert
def _alert(alert_id: str) -> dict:
    row = STATE["engine"].store.get(alert_id)
    if not row:
        raise HTTPException(404, "alert not found")
    return row


class NoteIn(BaseModel):
    kind: Literal["agent_assessment", "agent_action", "note", "patient_reply"]
    summary: str = Field(max_length=1000)
    details: dict[str, Any] | None = None


@router.post("/agent/alerts/{alert_id}/timeline")
def timeline(alert_id: str, body: NoteIn, caller: Caller) -> dict:
    row = _alert(alert_id)
    STATE["engine"].store.add_timeline(alert_id, body.kind, caller, body.summary, body.details)
    audit(caller, "update", "Flag", alert_id, row["patient_id"], {"op": body.kind})
    STATE["engine"].changed(alert_id)
    return {"ok": True}


class RaiseIn(BaseModel):
    severity: Literal["medium", "high", "critical"]
    reason: str = Field(max_length=500)


@router.post("/agent/alerts/{alert_id}/raise")
def raise_severity(alert_id: str, body: RaiseIn, caller: Caller) -> dict:
    """Raise only: the store refuses a severity that is not higher than the current one."""
    row = _alert(alert_id)
    raised = STATE["engine"].store.raise_severity(alert_id, body.severity, caller, body.reason)
    audit(caller, "update", "Flag", alert_id, row["patient_id"], {"op": "raise", "to": body.severity,
                                                                 "raised": raised})
    if raised:
        STATE["engine"].nudge()
        STATE["engine"].changed(alert_id)
    return {"raised": raised}


class ReasonIn(BaseModel):
    reason: str = Field(max_length=500)


@router.post("/agent/alerts/{alert_id}/escalate")
def escalate(alert_id: str, body: ReasonIn, caller: Caller) -> dict:
    row = _alert(alert_id)
    out = STATE["engine"].store.escalate(alert_id, caller, body.reason)
    audit(caller, "update", "Flag", alert_id, row["patient_id"], {"op": "escalate"})
    STATE["engine"].nudge()
    STATE["engine"].changed(alert_id)
    return {"tier": out["tier"] if out else None}


class BriefIn(BaseModel):
    brief: dict[str, Any] | None
    status: Literal["ready", "failed", "offline"]
    assessment: dict[str, Any] | None = None


@router.post("/agent/alerts/{alert_id}/brief")
def brief(alert_id: str, body: BriefIn, caller: Caller) -> dict:
    row = _alert(alert_id)
    STATE["engine"].store.attach_brief(alert_id, body.brief, body.status, body.assessment, caller)
    audit(caller, "create", "DocumentReference", alert_id, row["patient_id"], {"status": body.status})
    STATE["engine"].changed(alert_id)
    return {"ok": True}


class OpenAlertIn(BaseModel):
    event: ClinicalEvent


@router.post("/agent/alerts")
def open_alert(body: OpenAlertIn, caller: Caller) -> dict:
    """Open (or attach to) an alert for something the agents learnt from the patient, e.g. symptoms on a check-in."""
    ev = body.event.model_copy(update={"event_id": uuid7(), "requires_alert": True, "detected_at": utcnow()})
    res = STATE["engine"].store.ingest_event(ev)
    audit(caller, "create", "Flag", str(res.alert_id) if res else None, ev.patient_id, {"type": ev.type})
    if res:
        STATE["engine"].nudge()
        STATE["engine"].changed(res.alert_id)
    return {"alert_id": str(res.alert_id) if res else None}


# ------------------------------------------------------------------ agent tools: the patient's device
class DeviceIn(BaseModel):
    cmd: Literal["SPOT_CHECK", "CHECKIN", "SET_PROFILE"]
    args: dict[str, Any] = Field(default_factory=dict)


@router.post("/agent/patients/{patient_id}/device")
def device(patient_id: str, body: DeviceIn, caller: Caller) -> dict:
    with STATE["pool"].connection() as conn:
        row = conn.execute("SELECT s.device_id, p.care_setting FROM patient_status s JOIN patients p "
                           "USING (patient_id) WHERE s.patient_id = %s", (patient_id,)).fetchone()
    if not row or not row[0]:
        raise HTTPException(404, "no active device")
    if row[1] != "home" and body.cmd != "SET_PROFILE":
        raise HTTPException(409, "patient-facing commands apply to home patients only")
    cmd = STATE["commander"].send(row[0], body.cmd, body.args, caller)
    if body.cmd == "CHECKIN":
        with STATE["pool"].connection() as conn:
            conn.execute("INSERT INTO messages (patient_id, direction, kind, text, sender, device_id, cmd_id) "
                         "VALUES (%s, 'to_patient', 'checkin', %s, %s, %s, %s)",
                         (patient_id, str(body.args.get("text", ""))[:500], caller, row[0], cmd.cmd_id))
        publish(STATE["pool"], {"type": "message", "patient_id": patient_id})
    audit(caller, "create", "DeviceRequest", str(cmd.cmd_id), patient_id, {"cmd": body.cmd})
    return {"cmd_id": str(cmd.cmd_id), "device_id": row[0]}


# ------------------------------------------------------------------ agent runs
class RunIn(BaseModel):
    run_id: str
    task_id: str | None = None
    event_id: str | None = None
    patient_id: str
    alert_id: str | None = None
    trigger: str
    started_at: str
    status: Literal["succeeded", "degraded", "failed", "skipped"]
    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    assessment: dict[str, Any] | None = None
    actions: list[dict[str, Any]] = Field(default_factory=list)
    brief: dict[str, Any] | None = None
    trace: dict[str, Any] | None = None
    error: str | None = None


@router.post("/agent/runs")
def save_run(body: RunIn, caller: Caller) -> dict:
    with STATE["pool"].connection() as conn:
        conn.execute(
            "INSERT INTO agent_runs (run_id, event_id, patient_id, alert_id, trigger, started_at, finished_at, status, "
            "provider, model, input_tokens, output_tokens, assessment, actions, brief, trace, error) "
            "VALUES (%s,%s,%s,%s,%s,%s,now(),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (run_id) DO NOTHING",
            (body.run_id, body.event_id, body.patient_id, body.alert_id, body.trigger, body.started_at, body.status,
             body.provider, body.model, body.input_tokens, body.output_tokens,
             Jsonb(body.assessment) if body.assessment else None, Jsonb(body.actions),
             Jsonb(body.brief) if body.brief else None, Jsonb(body.trace) if body.trace else None, body.error))
    publish(STATE["pool"], {"type": "agent_run", "run_id": body.run_id, "patient_id": body.patient_id,
                            "alert_id": body.alert_id, "status": body.status, "trigger": body.trigger})
    return {"ok": True}


@router.get("/agent/models")
def models(caller: Caller, refresh: Annotated[bool, Query()] = False) -> list[dict]:
    if refresh:
        STATE["ctx"]._models = {}
    return list(STATE["ctx"].models().values())
