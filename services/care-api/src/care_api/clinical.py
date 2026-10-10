"""Clinician endpoints: alerts (acknowledge / escalate / resolve), agent output, risk, record, messages to the
patient, device assignment, and demonstration controls. Every read of patient data and every action is written to
audit_log with the acting user."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from rpm_core.contracts import AgentTask
from rpm_core.live import publish

from .alerts import ACTIVE
from .auth import Actor, Presenter, Reader, User

router = APIRouter(prefix="/api")
STATE: dict = {}  # filled by main.lifespan: pool, store, engine, ctx, commander, settings

CHECKIN_TEXT = {
    "breathless": "Are you more short of breath than usual right now?",
    "chest_pain": "Do you have chest pain, pressure or tightness right now?",
    "dizzy": "Do you feel dizzy, faint, confused, or is your heart racing?",
    "fever": "Do you feel feverish, shivery or have chills?",
}


def audit(request: Request, user: User, action: str, resource_type: str, resource_id: str | None,
          patient_id: str | None, details: dict | None = None) -> None:
    """Who (from the verified token, never from a client-supplied header) did what to which patient's data."""
    with STATE["pool"].connection() as conn:
        conn.execute(
            "INSERT INTO audit_log (actor, actor_role, action, resource_type, resource_id, patient_id, source_ip, "
            "details) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (user.username, user.role, action, resource_type, resource_id, patient_id,
             request.client.host if request.client else None,
             Jsonb({"path": request.url.path, "sub": user.sub, **(details or {})})))


def _rows(sql: str, params=()) -> list[dict]:
    with STATE["pool"].connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def _changed(alert_id) -> None:
    STATE["engine"].changed(alert_id)


# ------------------------------------------------------------------ alerts
@router.get("/alerts")
def list_alerts(user: Reader, status: Annotated[str, Query(pattern="^(active|resolved|all)$")] = "active",
                limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[dict]:
    statuses = {"active": ACTIVE, "resolved": ("resolved",), "all": (*ACTIVE, "resolved")}[status]
    return STATE["store"].list(statuses, limit)


@router.get("/alerts/{alert_id}")
def get_alert(alert_id: str, request: Request, user: Reader) -> dict:
    alert = STATE["store"].get(alert_id)
    if not alert:
        raise HTTPException(404, "alert not found")
    alert["agent_runs"] = _rows(
        "SELECT run_id, trigger, status, provider, model, started_at, finished_at, input_tokens, output_tokens, "
        "actions, error FROM agent_runs WHERE alert_id=%s ORDER BY started_at DESC LIMIT 10", (alert_id,))
    audit(request, user, "read", "Alert", alert_id, alert["patient_id"])
    return alert


class AckIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class ResolveIn(BaseModel):
    resolution: str = Field(min_length=3, max_length=500)


class EscalateIn(BaseModel):
    reason: str = Field(default="Escalated by clinician", max_length=500)


@router.post("/alerts/{alert_id}/ack")
def ack(alert_id: str, body: AckIn, request: Request, user: Actor) -> dict:
    row = STATE["store"].acknowledge(alert_id, user.username, body.note)
    if not row:
        raise HTTPException(409, "alert is not open")
    audit(request, user, "update", "Alert", alert_id, row["patient_id"], {"op": "acknowledge"})
    _changed(alert_id)
    return {"status": row["status"]}


@router.post("/alerts/{alert_id}/escalate")
def escalate(alert_id: str, body: EscalateIn, request: Request, user: Actor) -> dict:
    row = STATE["store"].escalate(alert_id, f"user:{user.username}", body.reason)
    if not row:
        raise HTTPException(409, "alert is not active")
    audit(request, user, "update", "Alert", alert_id, row["patient_id"], {"op": "escalate"})
    _changed(alert_id)
    return {"status": row["status"], "tier": row["tier"]}


@router.post("/alerts/{alert_id}/resolve")
def resolve(alert_id: str, body: ResolveIn, request: Request, user: Actor) -> dict:
    row = STATE["store"].resolve(alert_id, user.username, body.resolution)
    if not row:
        raise HTTPException(409, "alert is not active")
    audit(request, user, "update", "Alert", alert_id, row["patient_id"], {"op": "resolve"})
    _changed(alert_id)
    return {"status": row["status"]}


# ------------------------------------------------------------------ patient context for the console
@router.get("/patients/{patient_id}/risk")
def risk(patient_id: str, request: Request, user: Reader) -> list[dict]:
    audit(request, user, "read", "RiskAssessment", None, patient_id)
    return STATE["ctx"].risk(patient_id)["risk_models"]


@router.get("/patients/{patient_id}/agent-runs")
def agent_runs(patient_id: str, user: Reader, limit: Annotated[int, Query(ge=1, le=50)] = 10) -> list[dict]:
    return _rows("SELECT run_id, alert_id, trigger, status, provider, model, started_at, finished_at, assessment, "
                 "actions, input_tokens, output_tokens, error FROM agent_runs WHERE patient_id=%s "
                 "ORDER BY started_at DESC LIMIT %s", (patient_id, limit))


@router.get("/patients/{patient_id}/summary")
def summary(patient_id: str, request: Request, user: Reader) -> dict:
    rows = _rows("SELECT doc_id, title, content, updated_at FROM rag_documents WHERE patient_id=%s "
                 "AND kind='discharge_summary'", (patient_id,))
    if not rows:
        raise HTTPException(404, "no summary indexed")
    audit(request, user, "read", "DocumentReference", rows[0]["doc_id"], patient_id)
    return rows[0]


@router.get("/knowledge/{chunk_id}")
def knowledge(chunk_id: str, request: Request, user: Reader) -> dict:
    """The exact source text behind a citation in an agent brief (evidence on demand)."""
    rows = _rows("SELECT c.chunk_id, c.heading, c.text, c.patient_id, d.title, d.kind FROM rag_chunks c "
                 "JOIN rag_documents d USING (doc_id) WHERE chunk_id=%s", (chunk_id,))
    if not rows:
        raise HTTPException(404, "unknown source")
    if rows[0]["patient_id"]:
        audit(request, user, "read", "DocumentReference", chunk_id, rows[0]["patient_id"])
    return rows[0]


@router.get("/models")
def models(user: Reader) -> list[dict]:
    return list(STATE["ctx"].models().values())


@router.get("/patients/{patient_id}/labs")
def labs(patient_id: str, request: Request, user: Reader) -> list[dict]:
    audit(request, user, "read", "Observation", None, patient_id, {"category": "laboratory"})
    return _rows("SELECT ts, code, name, value, unit, flag FROM labs WHERE patient_id=%s ORDER BY ts DESC LIMIT 50",
                 (patient_id,))


@router.post("/patients/{patient_id}/agent-review", status_code=202)
def agent_review(patient_id: str, request: Request, user: Actor) -> dict:
    """Ask the agents for a fresh assessment and summary of this patient."""
    if STATE["ctx"].profile(patient_id) is None:
        raise HTTPException(404, "patient not found")
    active = STATE["store"].active_for_patient(patient_id)
    task = AgentTask(trigger="on_demand", patient_id=patient_id, requested_by=f"user:{user.username}",
                     alert_id=str(active["alert_id"]) if active else None)
    queued = STATE["engine"].enqueue(task)
    audit(request, user, "create", "Task", str(task.task_id), patient_id, {"op": "agent_review"})
    return {"queued": bool(queued), "task_id": str(task.task_id)}


# ------------------------------------------------------------------ the patient's device
def _device(patient_id: str) -> dict:
    rows = _rows("SELECT s.device_id, p.care_setting FROM patient_status s JOIN patients p USING (patient_id) "
                 "WHERE s.patient_id=%s", (patient_id,))
    if not rows or not rows[0]["device_id"]:
        raise HTTPException(404, "no active device")
    return rows[0]


def _message(patient_id: str, kind: str, text: str, sender: str, device_id: str, cmd_id) -> None:
    with STATE["pool"].connection() as conn:
        conn.execute("INSERT INTO messages (patient_id, direction, kind, text, sender, device_id, cmd_id) "
                     "VALUES (%s, 'to_patient', %s, %s, %s, %s, %s)",
                     (patient_id, kind, text, sender, device_id, cmd_id))
    publish(STATE["pool"], {"type": "message", "patient_id": patient_id})


class CommandIn(BaseModel):
    cmd: str = Field(pattern="^(SPOT_CHECK|CHECKIN|SET_PROFILE)$")
    topic: str | None = Field(default=None, pattern="^(breathless|chest_pain|dizzy|fever)$")
    profile: str | None = Field(default=None, pattern="^(normal|elevated|critical)$")


@router.post("/patients/{patient_id}/commands")
def command(patient_id: str, body: CommandIn, request: Request, user: Actor) -> dict:
    dev = _device(patient_id)
    if dev["care_setting"] != "home" and body.cmd != "SET_PROFILE":
        raise HTTPException(409, "ICU monitors have no patient screen; only the sampling profile can be changed")
    args = {"metric": "spo2"} if body.cmd == "SPOT_CHECK" else \
        {"profile": body.profile or "elevated"} if body.cmd == "SET_PROFILE" else \
        {"question_id": f"{body.topic or 'breathless'}-clin", "text": CHECKIN_TEXT[body.topic or "breathless"],
         "options": ["Yes", "No"]}
    cmd = STATE["commander"].send(dev["device_id"], body.cmd, args, f"user:{user.username}")
    if body.cmd == "CHECKIN":
        _message(patient_id, "checkin", args["text"], f"user:{user.username}", dev["device_id"], cmd.cmd_id)
    audit(request, user, "create", "DeviceRequest", str(cmd.cmd_id), patient_id, {"cmd": body.cmd})
    return {"cmd_id": str(cmd.cmd_id), "device_id": dev["device_id"]}


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=200)


@router.get("/patients/{patient_id}/messages")
def messages(patient_id: str, request: Request, user: Reader) -> list[dict]:
    audit(request, user, "read", "Communication", None, patient_id)
    return _rows("SELECT at, direction, kind, text, sender FROM messages WHERE patient_id=%s ORDER BY at DESC LIMIT 50",
                 (patient_id,))


@router.post("/patients/{patient_id}/messages")
def send_message(patient_id: str, body: MessageIn, request: Request, user: Actor) -> dict:
    """A clinician's own words to the patient's watch (home patients). Replies come back as check-in answers."""
    dev = _device(patient_id)
    if dev["care_setting"] != "home":
        raise HTTPException(409, "messages go to home patients' watches")
    cmd = STATE["commander"].send(dev["device_id"], "NOTIFY", {"title": "Message from your care team",
                                                               "text": body.text, "priority": "normal"},
                                  f"user:{user.username}", ttl_s=1800)
    _message(patient_id, "message", body.text, f"user:{user.username}", dev["device_id"], cmd.cmd_id)
    audit(request, user, "create", "Communication", str(cmd.cmd_id), patient_id)
    return {"cmd_id": str(cmd.cmd_id), "device_id": dev["device_id"]}


class DeviceAssignIn(BaseModel):
    device_id: str = Field(pattern=r"^[A-Za-z0-9_-]{3,64}$")   # the IoT thing name of the watch


@router.post("/patients/{patient_id}/device")
def assign_device(patient_id: str, body: DeviceAssignIn, request: Request, user: Actor) -> dict:
    """Give a home patient a real watch: it replaces whatever wearable was assigned before."""
    rows = _rows("SELECT care_setting FROM patients WHERE patient_id=%s", (patient_id,))
    if not rows:
        raise HTTPException(404, "patient not found")
    if rows[0]["care_setting"] != "home":
        raise HTTPException(409, "watches are assigned to home patients")
    with STATE["pool"].connection() as conn, conn.transaction():
        conn.execute("UPDATE device_assignments SET active_to = now() WHERE active_to IS NULL "
                     "AND (device_id = %s OR (patient_id = %s AND device_kind IN ('watch', 'simulator')))",
                     (body.device_id, patient_id))
        conn.execute("INSERT INTO device_assignments (device_id, patient_id, device_kind) VALUES (%s, %s, 'watch')",
                     (body.device_id, patient_id))
        conn.execute("UPDATE patient_status SET device_id = %s, latest = '{}'::jsonb, last_seen = NULL "
                     "WHERE patient_id = %s", (body.device_id, patient_id))
    audit(request, user, "update", "Device", body.device_id, patient_id, {"op": "assign"})
    return {"device_id": body.device_id, "patient_id": patient_id}


# ------------------------------------------------------------------ demonstration controls
def _demo() -> None:
    if not STATE["settings"].demo_controls:
        raise HTTPException(403, "demo controls disabled")


def _fhir() -> httpx.Client:
    base = STATE["settings"].fhir_base_url
    if not base:
        raise HTTPException(503, "FHIR server not configured")
    return httpx.Client(base_url=base.rstrip("/"), timeout=10,
                        headers={"Content-Type": "application/fhir+json", "Accept": "application/fhir+json"})


def _sim(payload: dict) -> None:
    STATE["commander"].publish_raw("rpm/v1/sim/control", json.dumps(payload))


class ScenarioIn(BaseModel):
    patient_id: str
    scenario: str = Field(pattern="^[a-z_]{3,40}$")
    ramp_s: float | None = Field(default=None, ge=1, le=1800)
    duration_s: float | None = Field(default=None, ge=5, le=7200)


@router.post("/demo/scenario")
def scenario(body: ScenarioIn, request: Request, user: Presenter) -> dict:
    _demo()
    payload = {"action": "scenario", "target": body.patient_id, "scenario": body.scenario,
               "ramp_s": body.ramp_s, "duration_s": body.duration_s}
    _sim(payload)
    audit(request, user, "create", "DemoScenario", body.scenario, body.patient_id, payload)
    return {"sent": payload}


@router.post("/demo/clear")
def clear(body: ScenarioIn, request: Request, user: Presenter) -> dict:
    _demo()
    _sim({"action": "clear", "target": body.patient_id})
    return {"sent": True}


class SettingIn(BaseModel):
    patient_id: str
    disposition: str | None = Field(default="Home", max_length=80)


def _open_encounters(http: httpx.Client, patient_id: str) -> list[dict]:
    r = http.get("/Encounter", params={"patient": patient_id, "status": "in-progress", "_count": 20})
    r.raise_for_status()
    return [e["resource"] for e in r.json().get("entry", [])]


@router.post("/demo/discharge")
def discharge(body: SettingIn, request: Request, user: Presenter) -> dict:
    """Discharge an ICU patient the way a hospital system would: by finishing the FHIR Encounter. The FHIR server's
    subscription puts the change on the patient-events stream; nothing here touches the operational store."""
    _demo()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with _fhir() as http:
        open_ = _open_encounters(http, body.patient_id) or [{
            "resourceType": "Encounter", "class": {"code": "IMP"},
            "subject": {"reference": f"Patient/{body.patient_id}"}, "period": {"start": now}}]
        ids = []
        for enc in sorted(open_, key=lambda e: "partOf" not in e):  # ICU stays first, the admission last
            enc["status"] = "finished"
            enc.setdefault("period", {})["end"] = now
            enc.setdefault("hospitalization", {})["dischargeDisposition"] = {"text": body.disposition}
            enc.pop("meta", None)
            r = http.put(f"/Encounter/{enc['id']}", json=enc) if enc.get("id") else http.post("/Encounter", json=enc)
            r.raise_for_status()
            ids.append(r.json().get("id"))
    _sim({"action": "discharge", "target": body.patient_id})
    audit(request, user, "update", "Encounter", ids[-1], body.patient_id, {"op": "discharge", "encounters": ids})
    return {"encounters": ids, "status": "finished"}


@router.post("/demo/admit")
def admit(body: SettingIn, request: Request, user: Presenter) -> dict:
    _demo()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    enc = {"resourceType": "Encounter", "status": "in-progress", "class": {"code": "IMP", "display": "ICU admission"},
           "subject": {"reference": f"Patient/{body.patient_id}"}, "period": {"start": now}}
    with _fhir() as http:
        r = http.post("/Encounter", json=enc)
        r.raise_for_status()
    _sim({"action": "admit", "target": body.patient_id})
    audit(request, user, "create", "Encounter", r.json().get("id"), body.patient_id, {"op": "admit"})
    return {"encounter": r.json().get("id"), "status": "in-progress"}


class LabIn(BaseModel):
    patient_id: str
    name: str = Field(min_length=2, max_length=80)
    value: float
    unit: str | None = Field(default=None, max_length=20)
    flag: str | None = Field(default=None, pattern="^(H|L|HH|LL|N|A)$")
    code: str | None = Field(default=None, max_length=40)


@router.post("/demo/lab")
def lab(body: LabIn, request: Request, user: Presenter) -> dict:
    """File a lab result in the hospital record (FHIR Observation), as the lab system would."""
    _demo()
    obs = {"resourceType": "Observation", "status": "final",
           "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                     "code": "laboratory"}]}],
           "code": {"text": body.name, "coding": [{"system": "http://loinc.org", "code": body.code or body.name}]},
           "subject": {"reference": f"Patient/{body.patient_id}"},
           "effectiveDateTime": datetime.now(UTC).isoformat(timespec="seconds"),
           "valueQuantity": {"value": body.value, **({"unit": body.unit} if body.unit else {})}}
    if body.flag:
        obs["interpretation"] = [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation", "code": body.flag}]}]
    with _fhir() as http:
        r = http.post("/Observation", json=obs)
        r.raise_for_status()
    audit(request, user, "create", "Observation", r.json().get("id"), body.patient_id, {"op": "lab"})
    return {"observation": r.json().get("id")}
