"""Assemble features for a patient from the operational store and score every applicable model."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from rpm_contracts import utcnow
from rpm_features.features import (
    aggregate_vitals,
    icu_mortality_features,
    instability_features,
    readmission_features,
)

from .registry import Registry

OUTCOME_TEXT = {"readmission_30d": "30-day hospital readmission", "icu_mortality": "In-hospital mortality",
                "instability_6h": "Clinical instability needing escalation within 6 h"}


def applicable(care_setting: str) -> list[str]:
    return ["icu_mortality", "instability_6h"] if care_setting == "icu" else ["readmission_30d", "instability_6h"]


class Scorer:
    def __init__(self, pool: ConnectionPool, registry: Registry, fhir_base: str | None):
        self.pool, self.registry, self.fhir_base = pool, registry, fhir_base

    def _load(self, patient_id: str) -> tuple[dict, list[dict], list[dict]]:
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            profile = cur.execute("SELECT * FROM patients WHERE patient_id=%s", (patient_id,)).fetchone()
            if not profile:
                raise KeyError(patient_id)
            day = cur.execute(
                "SELECT ts, heart_rate, spo2, resp_rate, sbp, temp, gcs, news2, news2_partial FROM vitals "
                "WHERE patient_id=%s AND ts > now() - interval '24 hours' AND NOT late ORDER BY ts",
                (patient_id,)).fetchall()
        hour_ago = utcnow() - timedelta(hours=1)
        return profile, day, [r for r in day if r["ts"] >= hour_ago]

    def features(self, model: str, profile: dict, day: list[dict], hour: list[dict]) -> tuple[dict, list[str]]:
        if model == "readmission_30d":
            return readmission_features(profile, aggregate_vitals(day))
        if model == "icu_mortality":
            start = profile.get("icu_admit_at") or (day[0]["ts"] if day else utcnow())
            return icu_mortality_features(profile, aggregate_vitals(day), (utcnow() - start).total_seconds() / 3600)
        if model == "instability_6h":
            return instability_features(hour)
        raise KeyError(model)

    def score_patient(self, patient_id: str, persist: bool = True) -> list[dict]:
        profile, day, hour = self._load(patient_id)
        results = []
        for name in applicable(profile["care_setting"]):
            if name not in self.registry.models:
                continue
            pkg = self.registry.get(name)
            feats, imputed = self.features(name, profile, day, hour)
            pred = pkg.predict(feats)
            res = {"model": name, "version": pkg.version, "probability": pred.probability, "band": pred.band,
                   "top_factors": pred.top_factors, "validated": bool(pkg.meta.get("validated")),
                   "metrics": pkg.meta.get("metrics"), "imputed": imputed, "features": feats,
                   "computed_at": utcnow().isoformat(), "outcome": OUTCOME_TEXT.get(name, name)}
            if persist:
                res["fhir_id"] = self._write_fhir(patient_id, res)
                with self.pool.connection() as conn:
                    conn.execute(
                        "INSERT INTO risk_scores (patient_id, model, model_version, probability, band, top_factors, "
                        "features, fhir_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (patient_id, name, pkg.version, pred.probability, pred.band, Jsonb(pred.top_factors),
                         Jsonb({"values": feats, "imputed": imputed}), res["fhir_id"]))
            results.append(res)
        return results

    def _write_fhir(self, patient_id: str, res: dict) -> str | None:
        """FHIR RiskAssessment, one logical resource per patient+model (FHIR versioning keeps the history)."""
        if not self.fhir_base:
            return None
        rid = f"risk-{patient_id}-{res['model'].replace('_', '-')}"
        factors = "; ".join(f"{f['feature']}={f['value']} ({f['contribution']:+.3f})" for f in res["top_factors"])
        body = {
            "resourceType": "RiskAssessment", "id": rid, "status": "final",
            "meta": {"tag": [{"system": "urn:rpm:tags", "code": "baseline-model" if not res["validated"]
                              else "validated-model"}]},
            "subject": {"reference": f"Patient/{patient_id}"},
            "occurrenceDateTime": res["computed_at"],
            "method": {"text": f"{res['model']} v{res['version']}"
                               + ("" if res["validated"] else " (unvalidated baseline)")},
            "prediction": [{"outcome": {"text": res["outcome"]}, "probabilityDecimal": res["probability"],
                            "qualitativeRisk": {"text": res["band"]}}],
            "note": [{"text": f"Top factors: {factors or 'n/a'}"}],
        }
        try:
            r = httpx.put(f"{self.fhir_base}/RiskAssessment/{rid}", json=body, timeout=10,
                          headers={"Content-Type": "application/fhir+json"})
            r.raise_for_status()
            return rid
        except httpx.HTTPError:
            return None

    def latest(self, patient_id: str) -> list[dict]:
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                "SELECT DISTINCT ON (model) model, model_version AS version, probability, band, top_factors, "
                "computed_at, fhir_id FROM risk_scores WHERE patient_id=%s ORDER BY model, computed_at DESC",
                (patient_id,)).fetchall()


def iso(ts: datetime) -> str:
    return ts.isoformat()
