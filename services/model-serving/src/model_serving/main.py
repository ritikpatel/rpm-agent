from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from rpm_common.data import fetch_dir
from rpm_common.db import make_pool, wait_for_db
from rpm_common.logging import setup_logging
from rpm_common.settings import BaseServiceSettings

from .registry import Registry
from .scoring import Scorer


class ServingSettings(BaseServiceSettings):
    service_name: str = "model-serving"
    model_registry: str = "./models/registry"   # local dir or s3://bucket/prefix
    port: int = 8001


settings = ServingSettings()
state: dict = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging(settings.service_name, settings.log_level)
    wait_for_db(settings.database_url)
    state["registry"] = Registry(Path(fetch_dir(settings.model_registry)))
    state["pool"] = make_pool(settings.database_url, max_size=6)
    state["scorer"] = Scorer(state["pool"], state["registry"], settings.fhir_base_url)
    yield
    state["pool"].close()


app = FastAPI(title="RPM model serving", version="0.1.0", lifespan=lifespan)


class FeaturesIn(BaseModel):
    features: dict[str, float]


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "models": sorted(state["registry"].models)}


@app.get("/v1/models")
def models() -> list[dict]:
    return [m.describe() for m in state["registry"].models.values()]


@app.post("/v1/models/{name}/predict")
def predict(name: str, body: FeaturesIn) -> dict:
    try:
        pkg = state["registry"].get(name)
        p = pkg.predict(body.features)
    except KeyError as exc:
        raise HTTPException(404, f"unknown model {name}") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"model": name, "version": pkg.version, "probability": p.probability, "band": p.band,
            "top_factors": p.top_factors, "validated": bool(pkg.meta.get("validated"))}


@app.post("/v1/patients/{patient_id}/risk")
def score(patient_id: str) -> list[dict]:
    try:
        return state["scorer"].score_patient(patient_id)
    except KeyError as exc:
        raise HTTPException(404, f"unknown patient {patient_id}") from exc


@app.get("/v1/patients/{patient_id}/risk")
def latest(patient_id: str) -> list[dict]:
    return state["scorer"].latest(patient_id)


def main() -> None:
    uvicorn.run("model_serving.main:app", host="0.0.0.0", port=settings.port, log_config=None)


if __name__ == "__main__":
    main()
