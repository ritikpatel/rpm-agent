"""Patient context for the agents, served section by section so each agent reads only what its job needs.

Sources: the operational store (vitals, events, alerts, risk scores, labs) and FHIR (problem list, medications)."""

from __future__ import annotations

import logging

import httpx
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

SECTIONS = ("profile", "vitals", "events", "risk", "record")


class PatientContext:
    def __init__(self, pool: ConnectionPool, risk_url: str | None, fhir_base: str | None, token: str):
        self.pool, self.risk_url, self.fhir_base, self.token = pool, risk_url, fhir_base, token
        self._models: dict[str, dict] = {}

    def _q(self, sql: str, params=()) -> list[dict]:
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(sql, params).fetchall()

    def profile(self, patient_id: str) -> dict | None:
        rows = self._q(
            "SELECT patient_id, display_name, age, sex, primary_condition, primary_diagnosis, care_setting, "
            "discharge_at, icu_admit_at, "
            "los_days, prior_admissions_12m, drg_severity, discharge_disposition, hypercapnic_resp_failure, "
            "on_supplemental_o2 FROM patients WHERE patient_id = %s", (patient_id,))
        return rows[0] if rows else None

    # ------------------------------------------------------------------ sections
    def vitals(self, patient_id: str) -> dict:
        status = self._q(
            "SELECT device_id, last_seen, latest, news2, news2_band, news2_partial, news2_detail "
            "FROM patient_status WHERE patient_id = %s", (patient_id,))
        trend = self._q(
            """SELECT date_bin('30 minutes', ts, TIMESTAMPTZ '2000-01-01 00:00+00') AS bucket, count(*) AS windows,
                      round(avg(heart_rate)::numeric, 0) AS hr_avg, round(max(heart_rate)::numeric, 0) AS hr_max,
                      min(spo2) AS spo2_min, max(resp_rate) AS rr_max, min(sbp) AS sbp_min, max(temp) AS temp_max,
                      min(gcs) AS gcs_min, max(news2) AS news2_max,
                      count(*) FILTER (WHERE (quality->>'heart_rate')::float < 0.6) AS low_quality_windows,
                      bool_or(buffered) AS had_backfill
               FROM vitals WHERE patient_id = %s AND ts > now() - interval '6 hours'
               GROUP BY 1 ORDER BY 1""", (patient_id,))
        recent = self._q(
            "SELECT ts, round(heart_rate::numeric,0) AS hr, (quality->>'heart_rate')::float AS hr_q, spo2, "
            "resp_rate AS rr, sbp, temp, gcs, news2, buffered, late, activity FROM vitals "
            "WHERE patient_id = %s ORDER BY ts DESC LIMIT 12", (patient_id,))
        st = status[0] if status else {}
        latest = {k: v.get("v") for k, v in (st.get("latest") or {}).items() if isinstance(v, dict)}
        return {"current": {**{k: st.get(k) for k in ("device_id", "last_seen", "news2", "news2_band",
                                                       "news2_partial", "news2_detail")}, "latest": latest},
                "trend_6h": trend, "recent_windows": list(reversed(recent))}

    def events(self, patient_id: str) -> dict:
        events = self._q(
            "SELECT detected_at, type, severity, requires_alert, summary FROM clinical_events "
            "WHERE patient_id = %s AND detected_at > now() - interval '24 hours' ORDER BY detected_at DESC LIMIT 15",
            (patient_id,))
        alert = self._q(
            "SELECT alert_id, status, severity, tier, title, event_types, event_count, opened_at, acknowledged_by "
            "FROM alerts WHERE patient_id = %s AND status IN ('open','acknowledged','escalated')", (patient_id,))
        runs = self._q(
            "SELECT started_at, actions FROM agent_runs WHERE patient_id = %s AND started_at > now() - "
            "interval '2 hours' AND actions IS NOT NULL ORDER BY started_at DESC LIMIT 10", (patient_id,))
        messages = self._q("SELECT at, direction, kind, text, sender FROM messages WHERE patient_id = %s "
                           "AND at > now() - interval '24 hours' ORDER BY at DESC LIMIT 10", (patient_id,))
        return {"events_24h": events, "active_alert": alert[0] if alert else None,
                "recent_agent_actions": [a for run in runs for a in (run["actions"] or [])
                                         if a.get("status") == "done"],
                "messages_24h": messages}

    def models(self) -> dict[str, dict]:
        """The models the risk service serves now (name -> description), cached; {} if it cannot be reached."""
        if not self._models and self.risk_url:
            try:
                r = httpx.get(f"{self.risk_url}/v1/models", timeout=5, headers={"X-Service-Token": self.token})
                r.raise_for_status()
                self._models = {m["name"]: m for m in r.json()}
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("risk model list unavailable", extra={"error": str(exc)[:200]})
        return self._models

    def risk(self, patient_id: str, fresh: bool = False) -> dict:
        if fresh and self.risk_url:
            try:
                httpx.post(f"{self.risk_url}/v1/score", json={"patient_id": patient_id, "trigger": "on_demand"},
                           timeout=15, headers={"X-Service-Token": self.token}).raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("risk scoring unavailable", extra={"patient_id": patient_id, "error": str(exc)[:200]})
        meta = self.models()
        rows = self._q(
            "SELECT DISTINCT ON (model) model, model_version AS version, probability, band, top_factors, computed_at, "
            "features->'output' AS output FROM risk_scores WHERE patient_id = %s "
            "AND (%s::jsonb IS NULL OR model_version = %s::jsonb ->> model) ORDER BY model, computed_at DESC",
            (patient_id, *[Jsonb(current_versions(meta)) if meta else None] * 2))
        for r in rows:
            m = meta.get(r["model"], {})
            r.update(title=m.get("title"), outcome=m.get("outcome"), label=m.get("label"),
                     short_label=m.get("short_label"), validated=bool(m.get("validated")),
                     metrics=m.get("metrics"), horizon=m.get("horizon") or "short")
        return {"risk_models": rows}

    def record(self, patient_id: str) -> dict:
        out: dict = {"profile": self.profile(patient_id), "conditions": [], "medications": []}
        out["labs"] = self._q("SELECT ts, name, value, unit, flag FROM labs WHERE patient_id = %s "
                              "ORDER BY ts DESC LIMIT 15", (patient_id,))
        if not self.fhir_base:
            return out
        try:
            with httpx.Client(timeout=5, headers={"Accept": "application/fhir+json"}) as http:
                conds = http.get(f"{self.fhir_base}/Condition", params={"patient": patient_id, "_count": 12}).json()
                for e in conds.get("entry", []):
                    code = e["resource"].get("code", {})
                    text = code.get("text") or next((c.get("display") for c in code.get("coding", [])), None)
                    if text and text not in out["conditions"]:
                        out["conditions"].append(text)
                meds = http.get(f"{self.fhir_base}/MedicationRequest",
                                params={"patient": patient_id, "_count": 12, "_sort": "-authoredon"}).json()
                for e in meds.get("entry", []):
                    name = e["resource"].get("medicationCodeableConcept", {}).get("text")
                    if name and name not in out["medications"]:
                        out["medications"].append(name)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("FHIR record unavailable", extra={"patient_id": patient_id, "error": str(exc)[:200]})
        return out

    def section(self, patient_id: str, name: str, fresh: bool = False) -> dict:
        if name == "risk":
            return self.risk(patient_id, fresh)
        if name == "profile":
            return {"profile": self.profile(patient_id)}
        return getattr(self, name)(patient_id)


def current_versions(meta: dict[str, dict]) -> dict[str, str]:
    """name -> the version served now. Scores from an older version (a replaced model) are history, not current."""
    return {name: m["version"] for name, m in meta.items() if m.get("version")}

