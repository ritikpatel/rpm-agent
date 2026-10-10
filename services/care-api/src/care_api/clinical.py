"""Clinical workflow endpoints: alerts (ack / escalate / resolve), agent output, risk, record, device commands,
and presenter controls. Every PHI read and every action is written to audit_log with the acting user."""

from __future__ import annotations

import json
from typing import Annotated

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from care_api.alerts import ACTIVE
from rpm_core.live import publish

from .auth import Actor, Presenter, Reader, User

router = APIRouter(prefix="/api")
STATE: dict = {}  # filled by main.lifespan: pool, store, redis, commander, settings

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
    publish(STATE["redis"], {"type": "alert_changed", "alert_id": str(alert_id)})


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
def risk(patient_id: str, user: Reader) -> list[dict]:
    rows = _rows("SELECT DISTINCT ON (model) model, model_version, probability, band, top_factors, computed_at "
                 "FROM risk_scores WHERE patient_id=%s ORDER BY model, computed_at DESC", (patient_id,))
    models = {m["name"]: m for m in STATE.get("models") or []}
    for r in rows:
        meta = models.get(r["model"], {})
        r["validated"], r["metrics"] = meta.get("validated"), meta.get("metrics")
    return rows


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
    return STATE.get("models") or []


# ------------------------------------------------------------------ clinician-initiated device commands
class CommandIn(BaseModel):
    cmd: str = Field(pattern="^(SPOT_CHECK|CHECKIN|SET_PROFILE)$")
    topic: str | None = Field(default=None, pattern="^(breathless|chest_pain|dizzy|fever)$")
    profile: str | None = Field(default=None, pattern="^(normal|elevated|critical)$")


@router.post("/patients/{patient_id}/commands")
def command(patient_id: str, body: CommandIn, request: Request, user: Actor) -> dict:
    rows = _rows("SELECT s.device_id, d.gateway_id FROM patient_status s LEFT JOIN device_assignments d "
                 "ON d.device_id = s.device_id AND d.active_to IS NULL WHERE s.patient_id=%s", (patient_id,))
    if not rows or not rows[0]["device_id"]:
        raise HTTPException(404, "no active device")
    gateway = rows[0]["gateway_id"]
    if gateway and body.cmd != "SET_PROFILE":  # a bedside monitor has no patient UI and measures continuously
        raise HTTPException(409, "bedside monitors behind the ICU gateway only accept SET_PROFILE")
    args = {"metric": "spo2"} if body.cmd == "SPOT_CHECK" else \
        {"profile": body.profile or "elevated"} if body.cmd == "SET_PROFILE" else \
        {"question_id": f"{body.topic or 'breathless'}-clin", "text": CHECKIN_TEXT[body.topic or "breathless"],
         "options": ["Yes", "No"]}
    cmd = STATE["commander"].send(rows[0]["device_id"], body.cmd, args, f"user:{user.username}", via=gateway)
    audit(request, user, "create", "DeviceCommand", str(cmd.cmd_id), patient_id, {"cmd": body.cmd})
    return {"cmd_id": str(cmd.cmd_id), "device_id": rows[0]["device_id"], "via": gateway}


# ------------------------------------------------------------------ presenter controls (demo only)
class ScenarioIn(BaseModel):
    patient_id: str
    scenario: str = Field(pattern="^[a-z_]{3,40}$")
    ramp_s: float | None = Field(default=None, ge=1, le=1800)
    duration_s: float | None = Field(default=None, ge=5, le=7200)


@router.post("/demo/scenario")
def scenario(body: ScenarioIn, request: Request, user: Presenter) -> dict:
    if not STATE["settings"].demo_controls:
        raise HTTPException(403, "demo controls disabled")
    payload = {"action": "scenario", "target": body.patient_id, "scenario": body.scenario,
               "ramp_s": body.ramp_s, "duration_s": body.duration_s}
    STATE["commander"].publish_raw("rpm/v1/sim/control", json.dumps(payload))
    audit(request, user, "create", "DemoScenario", body.scenario, body.patient_id, payload)
    return {"sent": payload}


def load_models(serving_url: str | None) -> list[dict]:
    if not serving_url:
        return []
    try:
        return httpx.get(f"{serving_url}/v1/models", timeout=5).json()
    except (httpx.HTTPError, ValueError):
        return []
