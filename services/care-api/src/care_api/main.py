"""Care API: the one service the console, the stream processor and the agents talk to.

Public (`/api/*`, `/ws/live`): every endpoint except /healthz and /api/auth/* requires a verified access token
(auth.py): Cognito on AWS, a local dev issuer only when RPM_ENV=local. Every read of patient data and every action is
written to audit_log with the token's user.
Internal (`/internal/*`, `/hooks/fhir/*`): service token; see internal.py and fhir.py."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from contextlib import asynccontextmanager
from typing import Annotated

import psycopg
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from prometheus_client import make_asgi_app
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from rpm_core.aws import client
from rpm_core.db import make_pool, migrate, wait_for_db
from rpm_core.devices import DeviceCommander
from rpm_core.live import LIVE_CHANNEL
from rpm_core.logging import setup_logging
from rpm_core.rag import RagIndex, make_embedder

from . import clinical, fhir, internal
from .auth import AUTH, AuthError, DevIssuer, Reader, User, cognito_verifier, current_user
from .context import PatientContext, current_versions
from .engine import AlertEngine
from .settings import CareSettings

log = logging.getLogger("care_api")

settings = CareSettings()
state: dict = {}
LOCAL = settings.env == "local"
ROLE_CHOICES = ["nurse", "physician", "presenter", "admin", "physician+presenter+admin"]


def configure_auth(cfg: CareSettings) -> None:
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


class LiveHub:
    """One Postgres LISTEN connection fanned out to every connected console."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.clients: set[WebSocket] = set()

    async def run(self) -> None:
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(self.database_url, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {LIVE_CHANNEL}")
                    async for note in conn.notifies():
                        for ws in list(self.clients):
                            try:
                                await ws.send_text(note.payload)
                            except Exception:  # noqa: BLE001 - a gone client is dropped; others keep receiving
                                self.clients.discard(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect; consoles also poll, so a gap is not a loss
                log.warning("live listener reconnecting", extra={"error": str(exc)[:200]})
                await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging(settings.service_name, settings.log_level)
    configure_auth(settings)
    wait_for_db(settings.database_url)
    if settings.run_migrations:
        migrate(settings.database_url)
    pool = make_pool(settings.database_url, max_size=12)
    sqs = client("sqs", settings.aws_region, settings.sqs_endpoint)
    commander = DeviceCommander(mode=settings.device_command_mode, region=settings.aws_region,
                                iot_endpoint=settings.iot_endpoint, mqtt_host=settings.mqtt_host,
                                mqtt_port=settings.mqtt_port)
    engine = AlertEngine(settings, pool, sqs, commander)
    ctx = PatientContext(pool, settings.risk_service_url, settings.fhir_base_url, settings.service_token)
    rag = RagIndex(pool, make_embedder(settings.embeddings, settings.aws_region))
    shared = dict(pool=pool, sqs=sqs, engine=engine, ctx=ctx, rag=rag, commander=commander, settings=settings,
                  store=engine.store)
    state.update(shared)
    for module in (clinical, internal, fhir):
        module.STATE.update(shared)

    stop = threading.Event()
    threading.Thread(target=engine.run, name="alert-engine", daemon=True, args=(stop,)).start()
    if settings.fhir_writeback and settings.fhir_base_url:
        threading.Thread(target=fhir.write_back_loop, name="fhir-writeback", daemon=True,
                         args=(pool, settings.fhir_base_url, settings.fhir_writeback_interval_s, stop)).start()
    hub = state["hub"] = LiveHub(settings.database_url)
    listener = asyncio.create_task(hub.run())
    log.info("care-api started", extra={"auth": settings.auth_mode, "email": settings.email_backend,
                                        "embeddings": settings.embeddings})
    yield
    stop.set()
    listener.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await listener
    pool.close()


app = FastAPI(title="RPM Care API", version="0.2.0", lifespan=lifespan,
              docs_url="/docs" if LOCAL else None, redoc_url=None, openapi_url="/openapi.json" if LOCAL else None)
app.include_router(clinical.router)
app.include_router(internal.router)
app.include_router(fhir.router)
app.mount("/metrics", make_asgi_app())


@app.middleware("http")
async def no_store(request: Request, call_next):
    """Patient data must not be kept by browsers or intermediaries."""
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
        return {"mode": "dev", "roles": ROLE_CHOICES}
    base = settings.oidc_domain.rstrip("/")
    return {"mode": "oidc", "client_id": settings.oidc_client_id, "authorize_url": f"{base}/oauth2/authorize",
            "token_url": f"{base}/oauth2/token", "logout_url": f"{base}/logout", "scope": "openid email profile"}


class DevLoginIn(BaseModel):
    username: str = Field(pattern=r"^[a-z][a-z0-9._-]{2,40}$")
    role: str = Field(pattern=r"^(nurse|physician|presenter|admin)(\+(nurse|physician|presenter|admin)){0,3}$")


@app.post("/api/auth/dev/token")
def dev_token(body: DevLoginIn) -> dict:
    dev: DevIssuer | None = AUTH.get("dev")
    if dev is None:  # not even present outside local mode
        raise HTTPException(404, "not found")
    return {"access_token": dev.mint(body.username, body.role), "token_type": "Bearer", "expires_in": 8 * 3600}


@app.get("/api/auth/me")
def me(user: Annotated[User, Depends(current_user)]) -> dict:
    return {"username": user.username, "role": user.role, "roles": sorted(user.roles),
            "can": {p: user.can(p) for p in ("read", "act", "demo", "audit")},
            "demo_controls": settings.demo_controls}


# ---------------------------------------------------------------------- read APIs
@app.get("/api/patients")
def list_patients(user: Reader,
                  setting: Annotated[str | None, Query(pattern="^(home|icu)$")] = None) -> list[dict]:
    meta = state["ctx"].models()
    versions = Jsonb(current_versions(meta)) if meta else None
    rows = _query(
        """
        SELECT p.patient_id, p.display_name, p.sex, p.age, p.primary_condition, p.primary_diagnosis, p.data_origin,
               p.care_setting,
               p.discharge_at, p.icu_admit_at, s.device_id, s.last_seen, s.news2, s.news2_band, s.news2_partial,
               s.latest,
               (SELECT count(*) FROM clinical_events e WHERE e.patient_id = p.patient_id
                  AND e.detected_at > now() - interval '24 hours' AND e.requires_alert) AS alerts_24h,
               (SELECT jsonb_object_agg(r.model, jsonb_build_object('probability', r.probability, 'band', r.band))
                  FROM (SELECT DISTINCT ON (model) model, probability, band FROM risk_scores
                        WHERE patient_id = p.patient_id
                          AND (%(versions)s::jsonb IS NULL OR model_version = %(versions)s::jsonb ->> model)
                        ORDER BY model, computed_at DESC) r) AS risk
        FROM patients p LEFT JOIN patient_status s USING (patient_id)
        WHERE p.monitoring_active AND (%(setting)s::text IS NULL OR p.care_setting = %(setting)s)
        ORDER BY s.news2 DESC NULLS LAST, p.patient_id
        """,
        {"setting": setting, "versions": versions},
    )
    for row in rows:   # what each probability means, for the worklist badge
        for name, r in (row["risk"] or {}).items():
            r.update(label=meta.get(name, {}).get("label"), short_label=meta.get(name, {}).get("short_label"))
    return rows


@app.get("/api/patients/{patient_id}")
def get_patient(patient_id: str, request: Request, user: Reader) -> dict:
    rows = _query(
        "SELECT p.*, s.device_id, s.last_seen, s.latest, s.news2, s.news2_band, s.news2_partial, s.news2_detail "
        "FROM patients p LEFT JOIN patient_status s USING (patient_id) WHERE p.patient_id = %s", (patient_id,))
    if not rows:
        raise HTTPException(404, "patient not found")
    clinical.audit(request, user, "read", "Patient", patient_id, patient_id)
    return rows[0]


@app.get("/api/patients/{patient_id}/vitals")
def get_vitals(patient_id: str, request: Request, user: Reader, minutes: Annotated[int, Query(ge=1, le=10080)] = 60,
               limit: Annotated[int, Query(ge=1, le=5000)] = 1000) -> list[dict]:
    clinical.audit(request, user, "read", "Observation", None, patient_id)
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
               count(*) FILTER (WHERE p.care_setting = 'icu') AS icu,
               count(*) FILTER (WHERE p.care_setting = 'home') AS home,
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
          AND ingest_at IS NOT NULL
        """)[0]
    pages = _query(
        """
        SELECT count(*) AS pages_1h,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) / 1000.0 AS page_p95_s
        FROM notifications WHERE sent_at > now() - interval '1 hour' AND status = 'sent' AND cause = 'event'
          AND channel IN ('console', 'email')
        """)[0]
    events = _query(
        "SELECT severity, count(*) AS n FROM clinical_events WHERE detected_at > now() - interval '1 hour' "
        "GROUP BY severity")
    return {**overview, **latency, **pages, "events_1h": {r["severity"]: r["n"] for r in events}}


# ---------------------------------------------------------------------- live feed
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
    hub: LiveHub = state["hub"]
    hub.clients.add(ws)
    try:
        while True:
            await ws.receive_text()  # keepalive
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(ws)


def main() -> None:
    uvicorn.run("care_api.main:app", host=settings.host, port=settings.port, log_config=None, proxy_headers=True)


if __name__ == "__main__":
    main()
