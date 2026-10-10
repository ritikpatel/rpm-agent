from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from prometheus_client import make_asgi_app
from pydantic import BaseModel

from rpm_core.data import fetch_dir
from rpm_core.db import make_pool, wait_for_db
from rpm_core.live import publish
from rpm_core.logging import setup_logging
from rpm_core.settings import BaseServiceSettings

from .plugins import PluginRegistry
from .scoring import Scorer

log = logging.getLogger("risk_service")


class RiskSettings(BaseServiceSettings):
    service_name: str = "risk-service"
    models_uri: str = "./models/plugins"   # local directory or s3://bucket/prefix
    host: str = "0.0.0.0"
    port: int = 8001


settings = RiskSettings()
state: dict = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging(settings.service_name, settings.log_level)
    wait_for_db(settings.database_url)
    state["registry"] = PluginRegistry(Path(fetch_dir(settings.models_uri)))
    state["pool"] = make_pool(settings.database_url, max_size=6)
    state["scorer"] = Scorer(state["pool"], state["registry"])
    yield
    state["pool"].close()


app = FastAPI(title="RPM risk service", version="0.2.0", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())


def service(x_service_token: Annotated[str | None, Header()] = None) -> None:
    if not x_service_token or not hmac.compare_digest(x_service_token, settings.service_token):
        raise HTTPException(401, "service token required")


Service = Annotated[None, Depends(service)]


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "models": sorted(state["registry"].models), "rejected": state["registry"].errors}


@app.get("/v1/models")
def models(_: Service) -> list[dict]:
    return [m.describe() for m in state["registry"].models.values()]


class ScoreIn(BaseModel):
    patient_id: str
    trigger: Literal["vitals", "lab", "discharge", "daily", "on_demand"] = "on_demand"


@app.post("/v1/score")
def score(body: ScoreIn, _: Service) -> list[dict]:
    try:
        results = state["scorer"].score_patient(body.patient_id, body.trigger)
    except KeyError:
        raise HTTPException(404, "patient not found") from None
    if results:
        publish(state["pool"], {"type": "risk", "patient_id": body.patient_id,
                                "scores": [{k: r[k] for k in ("model", "probability", "band")} for r in results]})
    return results


@app.post("/v1/score/daily")
def score_daily(_: Service) -> dict:
    """Run the daily models for every patient at home (called by a scheduled job)."""
    done = 0
    for patient_id in state["scorer"].home_patients():
        done += bool(state["scorer"].score_patient(patient_id, "daily"))
    return {"patients_scored": done}


@app.get("/v1/patients/{patient_id}/risk")
def latest(patient_id: str, _: Service) -> list[dict]:
    return state["scorer"].latest(patient_id)


def main() -> None:
    uvicorn.run("risk_service.main:app", host=settings.host, port=settings.port, log_config=None)


if __name__ == "__main__":
    main()
