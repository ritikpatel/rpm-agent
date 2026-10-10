"""Backend-for-frontend: read APIs, clinical workflow (clinical.py), WebSocket live feed.

Every endpoint except /healthz and /api/auth/* requires a verified access token (auth.py): Cognito on AWS, a local
dev issuer only when RPM_ENV=local. Every PHI read and every action is written to audit_log with the token's user."""

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
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from rpm_alerts import AlertStore, EscalationPolicy
from rpm_common.db import make_pool, wait_for_db
from rpm_common.devices import DeviceCommander
from rpm_common.logging import setup_logging
from rpm_common.settings import BaseServiceSettings

from . import clinical
from .auth import AUTH, AuthError, DevIssuer, Reader, User, cognito_verifier, current_user

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
    auth_mode: str = "oidc"         # oidc (Cognito) | dev (local only: API-minted tokens, same verification path)
    oidc_issuer: str | None = None  # https://cognito-idp.<region>.amazonaws.com/<user-pool-id>
    oidc_client_id: str | None = None
    oidc_domain: str | None = None  # hosted UI base, https://<prefix>.auth.<region>.amazoncognito.com


settings = ApiSettings()
state: dict = {}
LOCAL = settings.env == "local"


def configure_auth(cfg: ApiSettings) -> None:
    """Fail closed: dev tokens never outside RPM_ENV=local, and OIDC mode must be fully configured."""
    if cfg.auth_mode == "dev":
        if cfg.env != "local":
            raise RuntimeError("RPM_AUTH_MODE=dev is only allowed with RPM_ENV=local")
        AUTH["dev"] = DevIssuer()
        AUTH["verifier"] = AUTH["dev"].verifier()
    elif cfg.auth_mode == "oidc":
        if not (cfg.oidc_issuer and cfg.oidc_client_id and cfg.oidc_domain):
            raise RuntimeError("RPM_AUTH_MODE=oidc needs RPM_OIDC_ISSUER, RPM_OIDC_CLIENT_ID and RPM_OIDC_DOMAIN")
        AUTH["dev"] = None
        AUTH["verifier"] = cognito_verifier(cfg.oidc_issuer, cfg.oidc_client_id)
    else:
        raise RuntimeError(f"unknown RPM_AUTH_MODE {cfg.auth_mode!r}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging(settings.service_name, settings.log_level)
    configure_auth(settings)
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


app = FastAPI(title="RPM Platform API", version="0.4.0", lifespan=lifespan,
              docs_url="/docs" if LOCAL else None, redoc_url=None, openapi_url="/openapi.json" if LOCAL else None)
app.include_router(clinical.router)


@app.middleware("http")
async def no_store(request: Request, call_next):
    """PHI must not be kept by browsers or intermediaries."""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    return response


def _query(sql: str, params: tuple | dict = ()) -> list[dict]:
    with state["pool"].connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


@app.get("/healthz")
def healthz() -> dict:
    _query("SELECT 1")
    return {"status": "ok"}


# ---------------------------------------------------------------------- auth
@app.get("/api/auth/config")
def auth_config() -> dict:
    """What the console needs to sign in: Cognito hosted UI endpoints (authorization code + PKCE), or dev mode."""
    if settings.auth_mode == "dev":
        return {"mode": "dev", "roles": ["nurse", "physician", "presenter", "admin"]}
    base = settings.oidc_domain.rstrip("/")
    return {"mode": "oidc", "client_id": settings.oidc_client_id, "authorize_url": f"{base}/oauth2/authorize",
            "token_url": f"{base}/oauth2/token", "logout_url": f"{base}/logout", "scope": "openid email profile"}


class DevLoginIn(BaseModel):
    username: str = Field(pattern=r"^[a-z][a-z0-9._-]{2,40}$")
    role: str = Field(pattern="^(nurse|physician|presenter|admin)$")


@app.post("/api/auth/dev/token")
def dev_token(body: DevLoginIn) -> dict:
    dev: DevIssuer | None = AUTH.get("dev")
    if dev is None:  # not even present outside local mode
        raise HTTPException(404, "not found")
    return {"access_token": dev.mint(body.username, body.role), "token_type": "Bearer", "expires_in": 8 * 3600}


@app.get("/api/auth/me")
def me(user: Annotated[User, Depends(current_user)]) -> dict:
    return {"username": user.username, "role": user.role, "roles": sorted(user.roles),
            "can": {p: user.can(p) for p in ("read", "act", "demo")}}


def _audit(request: Request, user: User, action: str, resource_type: str, resource_id: str | None,
           patient_id: str | None):
    with state["pool"].connection() as conn:
        conn.execute(
            "INSERT INTO audit_log (actor, actor_role, action, resource_type, resource_id, patient_id, source_ip, "
            "details) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (user.username, user.role, action, resource_type, resource_id, patient_id,
             request.client.host if request.client else None, Jsonb({"path": request.url.path, "sub": user.sub})),
        )


@app.get("/api/patients")
def list_patients(user: Reader,
                  setting: Annotated[str | None, Query(pattern="^(home|icu)$")] = None) -> list[dict]:
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
def get_patient(patient_id: str, request: Request, user: Reader) -> dict:
    rows = _query(
        "SELECT p.*, s.device_id, s.last_seen, s.latest, s.news2, s.news2_band, s.news2_partial, s.news2_detail "
        "FROM patients p LEFT JOIN patient_status s USING (patient_id) WHERE p.patient_id = %s", (patient_id,))
    if not rows:
        raise HTTPException(404, "patient not found")
    _audit(request, user, "read", "Patient", patient_id, patient_id)
    return rows[0]


@app.get("/api/patients/{patient_id}/vitals")
def get_vitals(patient_id: str, request: Request, user: Reader, minutes: Annotated[int, Query(ge=1, le=10080)] = 60,
               limit: Annotated[int, Query(ge=1, le=5000)] = 1000) -> list[dict]:
    _audit(request, user, "read", "Observation", None, patient_id)
    return _query(
        "SELECT ts, device_id, source, heart_rate, hrv_rmssd, spo2, resp_rate, sbp, dbp, temp, skin_temp, gcs, "
        "steps, activity, news2, news2_band, news2_partial, buffered, late FROM vitals "
        "WHERE patient_id = %s AND ts > now() - make_interval(mins => %s) ORDER BY ts DESC LIMIT %s",
        (patient_id, minutes, limit))


@app.get("/api/events")
def list_events(user: Reader, limit: Annotated[int, Query(ge=1, le=500)] = 50, patient_id: str | None = None,
                alerts_only: bool = False) -> list[dict]:
    return _query(
        "SELECT event_id, patient_id, device_id, type, severity, requires_alert, occurred_at, detected_at, summary "
        "FROM clinical_events WHERE (%(pid)s::text IS NULL OR patient_id = %(pid)s) "
        "AND (NOT %(alerts)s OR requires_alert) ORDER BY detected_at DESC LIMIT %(limit)s",
        {"pid": patient_id, "alerts": alerts_only, "limit": limit})


@app.get("/api/stats")
def stats(user: Reader) -> dict:
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


WS_PROTOCOL = "rpm.v1"


@app.websocket("/ws/live")
async def live(ws: WebSocket) -> None:
    # Browsers cannot set Authorization on a WebSocket: the token travels as the second subprotocol entry
    # (["rpm.v1", "<jwt>"]), never in the URL where proxies and access logs would keep it.
    offered = [p.strip() for p in ws.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
    token = next((p for p in offered if p != WS_PROTOCOL), None)
    try:
        user = AUTH["verifier"].verify(token) if token else None
    except AuthError:
        user = None
    if WS_PROTOCOL not in offered or user is None or not user.can("read"):
        await ws.close(code=1008)  # policy violation
        return
    await ws.accept(subprotocol=WS_PROTOCOL)
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
    """Ops live board for local development (signs itself in with a dev token); the console replaces it on AWS."""
    if AUTH.get("dev") is None:
        raise HTTPException(404, "not found")
    return resources.files("rpm_api").joinpath("static/live.html").read_text()


def main() -> None:
    uvicorn.run("rpm_api.main:app", host=settings.host, port=settings.port, log_config=None, proxy_headers=True)


if __name__ == "__main__":
    main()
