"""Backend-for-frontend. Week 1: read APIs + WebSocket live feed + an ops live board.
Auth (Cognito JWT + RBAC) lands in week 4; until then every PHI read is still written to audit_log."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from importlib import resources
from typing import Annotated

import redis
import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from rpm_alerts import AlertStore, EscalationPolicy
from rpm_common.db import make_pool, wait_for_db
from rpm_common.devices import DeviceCommander
from rpm_common.logging import setup_logging
from rpm_common.settings import BaseServiceSettings

from . import clinical

log = logging.getLogger("rpm_api")
LIVE_CHANNEL = "rpm:live"


class ApiSettings(BaseServiceSettings):
    service_name: str = "api"
    host: str = "0.0.0.0"
    port: int = 8000
    model_serving_url: str | None = "http://localhost:8001"
    device_command_mode: str = "mqtt"
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    iot_endpoint: str | None = None
    demo_controls: bool = True     # presenter scenario controls; disable outside demos
    console_dir: str | None = None  # built clinician console (served at /console)


settings = ApiSettings()
state: dict = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging(settings.service_name, settings.log_level)
    wait_for_db(settings.database_url)
    state["pool"] = make_pool(settings.database_url, max_size=10)
    state["redis"] = aioredis.from_url(settings.redis_url)
    clinical.STATE.update(
        pool=state["pool"], store=AlertStore(state["pool"], EscalationPolicy.load()),
        redis=redis.Redis.from_url(settings.redis_url), settings=settings,
        commander=DeviceCommander(mode=settings.device_command_mode, region=settings.aws_region,
                                  iot_endpoint=settings.iot_endpoint, mqtt_host=settings.mqtt_host,
                                  mqtt_port=settings.mqtt_port),
        models=clinical.load_models(settings.model_serving_url))
    yield
    state["pool"].close()
    await state["redis"].aclose()


app = FastAPI(title="RPM Platform API", version="0.2.0", lifespan=lifespan)
app.include_router(clinical.router)


def _query(sql: str, params: tuple | dict = ()) -> list[dict]:
    with state["pool"].connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def _audit(request: Request, action: str, resource_type: str, resource_id: str | None, patient_id: str | None):
    actor = request.headers.get("x-user", "anonymous-dev")
    with state["pool"].connection() as conn:
        conn.execute(
            "INSERT INTO audit_log (actor, actor_role, action, resource_type, resource_id, patient_id, source_ip, "
            "details) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (actor, request.headers.get("x-role", "unauthenticated"), action, resource_type, resource_id, patient_id,
             request.client.host if request.client else None, Jsonb({"path": request.url.path})),
        )


@app.get("/healthz")
def healthz() -> dict:
    _query("SELECT 1")
    return {"status": "ok"}


@app.get("/api/patients")
def list_patients(setting: Annotated[str | None, Query(pattern="^(home|icu)$")] = None) -> list[dict]:
    return _query(
        """
        SELECT p.patient_id, p.display_name, p.sex, p.age, p.primary_condition, p.care_setting,
               p.discharge_at, s.device_id, s.last_seen, s.news2, s.news2_band, s.news2_partial, s.latest,
               (SELECT count(*) FROM clinical_events e WHERE e.patient_id = p.patient_id
                  AND e.detected_at > now() - interval '24 hours' AND e.requires_alert) AS alerts_24h
        FROM patients p LEFT JOIN patient_status s USING (patient_id)
        WHERE p.monitoring_active AND (%(setting)s::text IS NULL OR p.care_setting = %(setting)s)
        ORDER BY s.news2 DESC NULLS LAST, p.patient_id
        """,
        {"setting": setting},
    )


@app.get("/api/patients/{patient_id}")
def get_patient(patient_id: str, request: Request) -> dict:
    rows = _query(
        "SELECT p.*, s.device_id, s.last_seen, s.latest, s.news2, s.news2_band, s.news2_partial, s.news2_detail "
        "FROM patients p LEFT JOIN patient_status s USING (patient_id) WHERE p.patient_id = %s", (patient_id,))
    if not rows:
        raise HTTPException(404, "patient not found")
    _audit(request, "read", "Patient", patient_id, patient_id)
    return rows[0]


@app.get("/api/patients/{patient_id}/vitals")
def get_vitals(patient_id: str, request: Request, minutes: Annotated[int, Query(ge=1, le=10080)] = 60,
               limit: Annotated[int, Query(ge=1, le=5000)] = 1000) -> list[dict]:
    _audit(request, "read", "Observation", None, patient_id)
    return _query(
        "SELECT ts, device_id, source, heart_rate, hrv_rmssd, spo2, resp_rate, sbp, dbp, temp, skin_temp, gcs, "
        "steps, activity, news2, news2_band, news2_partial, buffered, late FROM vitals "
        "WHERE patient_id = %s AND ts > now() - make_interval(mins => %s) ORDER BY ts DESC LIMIT %s",
        (patient_id, minutes, limit))


@app.get("/api/events")
def list_events(limit: Annotated[int, Query(ge=1, le=500)] = 50, patient_id: str | None = None,
                alerts_only: bool = False) -> list[dict]:
    return _query(
        "SELECT event_id, patient_id, device_id, type, severity, requires_alert, occurred_at, detected_at, summary "
        "FROM clinical_events WHERE (%(pid)s::text IS NULL OR patient_id = %(pid)s) "
        "AND (NOT %(alerts)s OR requires_alert) ORDER BY detected_at DESC LIMIT %(limit)s",
        {"pid": patient_id, "alerts": alerts_only, "limit": limit})


@app.get("/api/stats")
def stats() -> dict:
    overview = _query(
        """
        SELECT count(*) AS patients,
               count(*) FILTER (WHERE s.last_seen > now() - interval '2 minutes') AS streaming,
               count(*) FILTER (WHERE s.news2_band = 'high') AS band_high,
               count(*) FILTER (WHERE s.news2_band = 'medium') AS band_medium,
               count(*) FILTER (WHERE s.news2_band = 'low-medium') AS band_low_medium
        FROM patients p LEFT JOIN patient_status s USING (patient_id) WHERE p.monitoring_active
        """)[0]
    latency = _query(
        """
        SELECT count(*) AS windows_5m,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM processed_at - ts)) AS e2e_p50_s,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM processed_at - ts)) AS e2e_p95_s,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM processed_at - ingest_at))
                   AS pipeline_p95_s
        FROM vitals WHERE processed_at > now() - interval '5 minutes' AND NOT buffered AND NOT late
        """)[0]
    events = _query(
        "SELECT severity, count(*) AS n FROM clinical_events WHERE detected_at > now() - interval '1 hour' "
        "GROUP BY severity")
    return {**overview, **latency, "events_1h": {r["severity"]: r["n"] for r in events}}


@app.websocket("/ws/live")
async def live(ws: WebSocket) -> None:
    await ws.accept()
    pubsub = state["redis"].pubsub()
    await pubsub.subscribe(LIVE_CHANNEL)

    async def pump():
        async for msg in pubsub.listen():
            if msg["type"] == "message":
                await ws.send_text(msg["data"].decode())

    task = asyncio.create_task(pump())
    try:
        while True:
            await ws.receive_text()  # keepalive / future client commands
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(LIVE_CHANNEL)
            await pubsub.aclose()


@app.get("/", response_class=HTMLResponse)
def live_board() -> str:
    return resources.files("rpm_api").joinpath("static/live.html").read_text()


def main() -> None:
    uvicorn.run("rpm_api.main:app", host=settings.host, port=settings.port, log_config=None, proxy_headers=True)


if __name__ == "__main__":
    main()
