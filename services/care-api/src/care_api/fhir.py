"""The FHIR side of care-api.

In:  HAPI FHIR notifies this service (rest-hook Subscription) when the hospital record changes. Admissions,
     discharges and lab results are put on the unified patient-events queue in the same envelope as device data.
Out: risk scores (RiskAssessment), alerts (Flag) and the agents' summaries (DocumentReference) are written back, so
     any FHIR-capable hospital system can read what the platform produced. Write-back is an outbox loop: rows that
     have no FHIR id yet are retried until the server accepts them."""

from __future__ import annotations

import base64
import hmac
import json
import logging
import threading
import time
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response
from psycopg.rows import dict_row

from rpm_core.contracts import EhrEvent, utcnow

log = logging.getLogger(__name__)
router = APIRouter(prefix="/hooks/fhir")
STATE: dict = {}  # filled by main.lifespan: pool, sqs, settings

FHIR_JSON = {"Content-Type": "application/fhir+json", "Accept": "application/fhir+json"}
SYSTEM = "https://rpm.example/fhir"


def _ts(value: str | None) -> datetime:
    if not value:
        return utcnow()
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _patient(resource: dict) -> str | None:
    ref = (resource.get("subject") or resource.get("patient") or {}).get("reference", "")
    return ref.split("/", 1)[1] if ref.startswith("Patient/") else None


def to_ehr_event(resource: dict) -> EhrEvent | None:
    """Map a changed FHIR resource to the event the platform reacts to; None for changes it ignores."""
    rtype, rid = resource.get("resourceType"), resource.get("id")
    patient_id = _patient(resource)
    if not rid or not patient_id:
        return None
    version = (resource.get("meta") or {}).get("versionId", "1")
    msg_id = f"{rtype}/{rid}/{version}"
    if rtype == "Encounter":
        if resource.get("partOf"):
            return None  # an ICU stay inside an admission: the admission itself carries the change
        period = resource.get("period") or {}
        hosp = resource.get("hospitalization") or {}
        data: dict[str, Any] = {"encounter_id": rid, "class": (resource.get("class") or {}).get("code")}
        if resource.get("status") == "finished":
            start, end = period.get("start"), period.get("end")
            if start and end:
                data["los_days"] = round((_ts(end) - _ts(start)).total_seconds() / 86400, 2)
            data["disposition"] = (hosp.get("dischargeDisposition") or {}).get("text")
            return EhrEvent(msg_id=msg_id, type="discharge", patient_id=patient_id, occurred_at=_ts(end), data=data)
        if resource.get("status") in ("in-progress", "arrived"):
            return EhrEvent(msg_id=msg_id, type="admission", patient_id=patient_id,
                            occurred_at=_ts(period.get("start")), data=data)
        return None
    if rtype == "Observation":
        cats = {c.get("code") for cat in resource.get("category", []) for c in cat.get("coding", [])}
        if "laboratory" not in cats:
            return None
        code = resource.get("code") or {}
        coding = (code.get("coding") or [{}])[0]
        qty = resource.get("valueQuantity") or {}
        interp = ((resource.get("interpretation") or [{}])[0].get("coding") or [{}])[0]
        return EhrEvent(msg_id=msg_id, type="lab", patient_id=patient_id,
                        occurred_at=_ts(resource.get("effectiveDateTime") or resource.get("issued")),
                        data={"fhir_id": f"Observation/{rid}", "code": coding.get("code", ""),
                              "name": code.get("text") or coding.get("display") or coding.get("code", "lab"),
                              "value": qty.get("value"), "unit": qty.get("unit"), "flag": interp.get("code")})
    return None


def enqueue(ev: EhrEvent) -> None:
    """Put an EHR event on the unified stream, shaped like the IoT rule's output so one consumer handles both."""
    sqs, cfg = STATE["sqs"], STATE["settings"]
    if "queue_url" not in STATE:
        STATE["queue_url"] = sqs.get_queue_url(QueueName=cfg.sqs_events_queue)["QueueUrl"]
    body = {**ev.to_dict(), "thing": "fhir", "kind": "ehr", "ingest_ts_ms": int(time.time() * 1000)}
    sqs.send_message(QueueUrl=STATE["queue_url"], MessageBody=json.dumps(body))


async def _receive(request: Request, token: str | None) -> Response:
    """Always answers 200 with an empty body once authenticated: the FHIR server treats anything else (including a
    JSON body that is not a FHIR resource) as a failed delivery and keeps retrying."""
    if not token or not hmac.compare_digest(token, STATE["settings"].service_token):
        raise HTTPException(401, "service token required")
    try:
        resource = json.loads(await request.body())
    except ValueError as exc:
        raise HTTPException(400, "body is not JSON") from exc
    ev = to_ehr_event(resource) if isinstance(resource, dict) else None
    if ev is not None:
        with STATE["pool"].connection() as conn:
            known = conn.execute("SELECT 1 FROM patients WHERE patient_id = %s AND monitoring_active",
                                 (ev.patient_id,)).fetchone()
        if known:
            enqueue(ev)
            log.info("EHR event queued", extra={"type": ev.type, "patient_id": ev.patient_id, "msg_id": ev.msg_id})
    return Response(status_code=200)


# HAPI delivers a rest-hook notification with a payload as PUT {endpoint}/{ResourceType}/{id}.
@router.put("/{rtype}/{rid}")
async def hook_put(rtype: str, rid: str, request: Request,
                   x_service_token: Annotated[str | None, Header()] = None) -> Response:
    return await _receive(request, x_service_token)


@router.post("/{rtype}")
async def hook_post(rtype: str, request: Request,
                    x_service_token: Annotated[str | None, Header()] = None) -> Response:
    return await _receive(request, x_service_token)


# ---------------------------------------------------------------------- FHIR out
RISK_REFRESH_S = 900   # an unchanged band is rewritten to FHIR at most this often


def risk_id(patient_id: str, model: str) -> str:
    return f"risk-{patient_id}-{model.replace('_', '-')}"


def risk_assessment(row: dict) -> dict:
    """One logical RiskAssessment per patient and model; FHIR versioning keeps its history."""
    return {
        "resourceType": "RiskAssessment", "id": risk_id(row["patient_id"], row["model"]), "status": "final",
        "subject": {"reference": f"Patient/{row['patient_id']}"},
        "occurrenceDateTime": row["computed_at"].isoformat(),
        "method": {"text": f"{row['model']} v{row['model_version']}"},
        "code": {"coding": [{"system": f"{SYSTEM}/risk-model", "code": row["model"]}], "text": row["model"]},
        "prediction": [{"probabilityDecimal": round(float(row["probability"]), 4),
                        "qualitativeRisk": {"text": row["band"]}}],
        "note": [{"text": "Top factors: " + ", ".join(
            str(f.get("feature")) for f in (row["top_factors"] or [])[:5])}],
    }


def flag(row: dict) -> dict:
    return {
        "resourceType": "Flag", "status": "inactive" if row["status"] == "resolved" else "active",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/flag-category",
                                  "code": "clinical"}]}],
        "code": {"coding": [{"system": f"{SYSTEM}/alert-severity", "code": row["severity"]}], "text": row["title"]},
        "subject": {"reference": f"Patient/{row['patient_id']}"},
        "period": {"start": row["opened_at"].isoformat()},
        "identifier": [{"system": f"{SYSTEM}/alert", "value": str(row["alert_id"])}],
    }


def document_reference(row: dict) -> dict:
    b = row["brief"]
    text = "\n\n".join(f"{k.upper()}: {b.get(k, '')}" for k in
                       ("headline", "situation", "background", "assessment", "recommendation"))
    text += "\n\nSOURCES: " + ", ".join(c.get("ref", "") for c in b.get("citations", []))
    return {
        "resourceType": "DocumentReference", "status": "current",
        "type": {"text": "Remote monitoring SBAR summary"},
        "subject": {"reference": f"Patient/{row['patient_id']}"}, "date": utcnow().isoformat(),
        "description": b.get("headline", "Clinical summary")[:200],
        "identifier": [{"system": f"{SYSTEM}/alert-brief", "value": str(row["alert_id"])}],
        "content": [{"attachment": {"contentType": "text/plain",
                                    "data": base64.b64encode(text.encode()).decode()}}],
    }


def _write_risk(pool, http: httpx.Client) -> int:
    """The newest unwritten score of each patient and model. Scores arrive every minute; FHIR gets one when the band
    changes, otherwise at most every RISK_REFRESH_S, and the rows in between are marked as covered."""
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            """SELECT DISTINCT ON (r.patient_id, r.model) r.id, r.patient_id, r.model, r.model_version, r.probability,
                      r.band, r.top_factors, r.computed_at, w.band AS written_band, w.computed_at AS written_at
               FROM risk_scores r
               LEFT JOIN LATERAL (SELECT band, computed_at FROM risk_scores p WHERE p.patient_id = r.patient_id
                                  AND p.model = r.model AND p.fhir_id IS NOT NULL
                                  ORDER BY computed_at DESC LIMIT 1) w ON true
               WHERE r.fhir_id IS NULL AND r.computed_at > now() - interval '1 day'
               ORDER BY r.patient_id, r.model, r.computed_at DESC LIMIT 50""").fetchall()
    done = 0
    for r in rows:
        rid = f"RiskAssessment/{risk_id(r['patient_id'], r['model'])}"
        fresh = r["written_at"] is not None and r["written_band"] == r["band"] and \
            (r["computed_at"] - r["written_at"]).total_seconds() < RISK_REFRESH_S
        if not fresh:
            http.put(f"/{rid}", json=risk_assessment(r)).raise_for_status()
            done += 1
        with pool.connection() as conn:
            conn.execute("UPDATE risk_scores SET fhir_id = %s WHERE patient_id = %s AND model = %s "
                         "AND fhir_id IS NULL AND computed_at <= %s", (rid, r["patient_id"], r["model"],
                                                                      r["computed_at"]))
    return done


def write_back_once(pool, http: httpx.Client) -> int:
    done = _write_risk(pool, http)
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        alerts = cur.execute("SELECT alert_id, patient_id, status, severity, title, opened_at FROM alerts "
                             "WHERE fhir_flag_id IS NULL ORDER BY opened_at LIMIT 25").fetchall()
        briefs = cur.execute("SELECT alert_id, patient_id, brief FROM alerts WHERE fhir_brief_id IS NULL "
                             "AND brief IS NOT NULL AND brief_status IN ('ready', 'offline') LIMIT 25").fetchall()
    jobs = ([("Flag", flag(r), "UPDATE alerts SET fhir_flag_id = %s WHERE alert_id = %s", r["alert_id"])
             for r in alerts]
            + [("DocumentReference", document_reference(r),
                "UPDATE alerts SET fhir_brief_id = %s WHERE alert_id = %s", r["alert_id"]) for r in briefs])
    for rtype, resource, sql, key in jobs:
        r = http.post(f"/{rtype}", json=resource)
        r.raise_for_status()
        with pool.connection() as conn:
            conn.execute(sql, (f"{rtype}/{r.json()['id']}", key))
        done += 1
    return done


def write_back_loop(pool, fhir_base: str, interval_s: float, stop: threading.Event) -> None:
    http = httpx.Client(base_url=fhir_base.rstrip("/"), timeout=10, headers=FHIR_JSON)
    while not stop.wait(interval_s):
        try:
            write_back_once(pool, http)
        except Exception as exc:  # noqa: BLE001 - FHIR being down must not disturb alerting; rows stay pending
            log.warning("FHIR write-back deferred", extra={"error": str(exc)[:200]})
