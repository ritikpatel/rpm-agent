"""Build the patient context from the operational store, run every plug-in that applies, store the scores."""

from __future__ import annotations

import logging

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from rpm_core.contracts import utcnow

from .plugins import PluginRegistry

log = logging.getLogger(__name__)

VITAL_COLUMNS = ("ts, heart_rate, hrv_rmssd, spo2, resp_rate, sbp, dbp, temp, skin_temp, gcs, steps, activity, "
                 "news2, news2_partial, context, source")


class Scorer:
    def __init__(self, pool: ConnectionPool, registry: PluginRegistry, max_rows: int = 4000):
        self.pool, self.registry, self.max_rows = pool, registry, max_rows

    def context(self, patient_id: str) -> dict:
        """The standard input every plug-in's build(ctx) receives (documented in models/CONTRACT.md)."""
        now = utcnow()
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            profile = cur.execute(
                "SELECT patient_id, age, sex, primary_condition, care_setting, discharge_at, icu_admit_at, los_days, "
                "prior_admissions_12m, drg_severity, discharge_disposition, hypercapnic_resp_failure, "
                "on_supplemental_o2, record_facts, last_recorded, discharge_vitals FROM patients "
                "WHERE patient_id = %s", (patient_id,)).fetchone()
            if not profile:
                raise KeyError(patient_id)
            record = profile.pop("record_facts") or {}
            last_recorded = profile.pop("last_recorded") or {}
            discharge_vitals = profile.pop("discharge_vitals") or {}
            vitals = cur.execute(
                f"SELECT {VITAL_COLUMNS} FROM (SELECT {VITAL_COLUMNS} FROM vitals WHERE patient_id = %s "
                "AND ts > now() - interval '30 hours' AND NOT late ORDER BY ts DESC LIMIT %s) v ORDER BY ts",
                (patient_id, self.max_rows)).fetchall()
            daily = cur.execute(
                """SELECT date_trunc('day', ts) AS day, count(*) AS windows, avg(heart_rate) AS hr_mean,
                          min(heart_rate) AS hr_min, max(heart_rate) AS hr_max, avg(spo2) AS spo2_mean,
                          min(spo2) AS spo2_min, max(resp_rate) AS rr_max, max(temp) AS temp_max,
                          avg(hrv_rmssd) AS hrv_mean, sum(steps) AS steps, max(news2) AS news2_max,
                          avg(news2) AS news2_mean
                   FROM vitals WHERE patient_id = %s AND ts > now() - interval '7 days' AND NOT late
                   GROUP BY 1 ORDER BY 1""", (patient_id,)).fetchall()
            labs = cur.execute("SELECT ts, code, name, value, unit, flag FROM labs WHERE patient_id = %s "
                               "AND ts > now() - interval '7 days' ORDER BY ts", (patient_id,)).fetchall()
        def hours(since):
            return None if since is None else round((now - since).total_seconds() / 3600, 2)

        return {"now": now, "patient_id": patient_id, "care_setting": profile["care_setting"], "profile": profile,
                "vitals": vitals, "daily": daily, "labs": labs, "record": record, "last_recorded": last_recorded,
                "discharge_vitals": discharge_vitals,
                "hours_since_discharge": hours(profile["discharge_at"]) if profile["care_setting"] == "home" else None,
                "hours_in_icu": hours(profile["icu_admit_at"]) if profile["care_setting"] == "icu" else None}

    def _recent(self, patient_id: str) -> dict[str, float]:
        """Seconds since each model last scored this patient."""
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT model, extract(epoch FROM now() - max(computed_at)) FROM risk_scores "
                                "WHERE patient_id = %s GROUP BY model", (patient_id,)).fetchall()
        return {m: float(age) for m, age in rows}

    def score_patient(self, patient_id: str, trigger: str = "on_demand", persist: bool = True) -> list[dict]:
        ctx = self.context(patient_id)
        age = self._recent(patient_id) if trigger == "vitals" else {}
        results = []
        for name, plugin in self.registry.models.items():
            if not plugin.applies(trigger, ctx["care_setting"], ctx):
                continue
            if age.get(name, float("inf")) < plugin.min_interval_s:
                continue
            try:
                pred = plugin.predict(plugin.build(ctx))
                output = plugin.explain(ctx, pred)
            except Exception as exc:  # noqa: BLE001 - one model failing must not hide the others' scores
                log.error("model failed", extra={"model": name, "patient_id": patient_id, "error": str(exc)[:300]})
                continue
            res = {"model": name, "version": plugin.version, "title": plugin.meta["title"],
                   "outcome": plugin.meta["outcome"], "probability": pred.probability, "band": pred.band,
                   "top_factors": pred.top_factors, "validated": bool(plugin.meta.get("validated")),
                   "output": output, "trigger": trigger, "computed_at": utcnow().isoformat()}
            if persist:
                with self.pool.connection() as conn:
                    conn.execute(
                        "INSERT INTO risk_scores (patient_id, model, model_version, probability, band, top_factors, "
                        "features) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (patient_id, name, plugin.version, pred.probability, pred.band, Jsonb(pred.top_factors),
                         Jsonb({"values": pred.features, "trigger": trigger, "output": output})))
            results.append(res)
        return results

    def latest(self, patient_id: str) -> list[dict]:
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                "SELECT DISTINCT ON (model) model, model_version AS version, probability, band, top_factors, "
                "computed_at, fhir_id FROM risk_scores WHERE patient_id = %s ORDER BY model, computed_at DESC",
                (patient_id,)).fetchall()

    def home_patients(self) -> list[str]:
        with self.pool.connection() as conn:
            return [r[0] for r in conn.execute(
                "SELECT patient_id FROM patients WHERE monitoring_active AND care_setting = 'home'")]
