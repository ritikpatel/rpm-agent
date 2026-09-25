"""Patient context for the agents: profile, live state, 6 h trend, recent events, active alert, risk models, record.

Everything is read-only and rendered into labelled blocks the prompts refer to ([PROFILE], [TREND_6H], ...), so
each statement in a brief can be traced to a block or a retrieved document chunk.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import httpx
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)


def _j(v: Any) -> str:
    return json.dumps(v, default=lambda o: o.isoformat() if isinstance(o, datetime) else str(o), separators=(",", ":"))


class ContextBuilder:
    def __init__(self, pool: ConnectionPool, serving_url: str | None, fhir_base: str | None):
        self.pool, self.serving_url, self.fhir_base = pool, serving_url, fhir_base

    def build(self, patient_id: str, score_risk: bool = True) -> dict:
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            profile = cur.execute(
                "SELECT patient_id, display_name, age, sex, primary_condition, care_setting, discharge_at, "
                "icu_admit_at, los_days, prior_admissions_12m, drg_severity, discharge_disposition, "
                "hypercapnic_resp_failure, on_supplemental_o2, thresholds FROM patients WHERE patient_id=%s",
                (patient_id,)).fetchone()
            if profile is None:
                raise KeyError(patient_id)
            status = cur.execute(
                "SELECT device_id, last_seen, latest, news2, news2_band, news2_partial, news2_detail "
                "FROM patient_status WHERE patient_id=%s", (patient_id,)).fetchone() or {}
            trend = cur.execute(
                """SELECT date_bin('30 minutes', ts, TIMESTAMPTZ '2000-01-01 00:00+00') AS bucket, count(*) AS windows,
                          round(avg(heart_rate)::numeric, 0) AS hr_avg, round(max(heart_rate)::numeric, 0) AS hr_max,
                          min(spo2) AS spo2_min, max(resp_rate) AS rr_max, min(sbp) AS sbp_min, max(temp) AS temp_max,
                          min(gcs) AS gcs_min, max(news2) AS news2_max,
                          count(*) FILTER (WHERE (quality->>'heart_rate')::float < 0.6) AS low_quality_windows,
                          bool_or(buffered) AS had_backfill
                   FROM vitals WHERE patient_id=%s AND ts > now() - interval '6 hours'
                   GROUP BY 1 ORDER BY 1""", (patient_id,)).fetchall()
            recent = cur.execute(
                "SELECT ts, round(heart_rate::numeric,0) AS hr, (quality->>'heart_rate')::float AS hr_q, spo2, "
                "resp_rate AS rr, sbp, temp, gcs, news2, buffered, late, activity FROM vitals "
                "WHERE patient_id=%s ORDER BY ts DESC LIMIT 12", (patient_id,)).fetchall()
            events = cur.execute(
                "SELECT detected_at, type, severity, requires_alert, summary FROM clinical_events "
                "WHERE patient_id=%s AND detected_at > now() - interval '24 hours' ORDER BY detected_at DESC LIMIT 15",
                (patient_id,)).fetchall()
            alert = cur.execute(
                "SELECT alert_id, status, severity, tier, title, event_types, event_count, opened_at, "
                "acknowledged_by FROM alerts WHERE patient_id=%s AND status IN ('open','acknowledged','escalated')",
                (patient_id,)).fetchone()
            actions = cur.execute(
                "SELECT started_at, actions FROM agent_runs WHERE patient_id=%s AND started_at > now() - "
                "interval '2 hours' AND actions IS NOT NULL ORDER BY started_at DESC LIMIT 10",
                (patient_id,)).fetchall()
        return {
            "profile": profile, "status": status, "trend": trend, "recent": list(reversed(recent)),
            "events": events, "alert": alert,
            "recent_actions": [a for run in actions for a in (run["actions"] or []) if a.get("status") == "done"],
            "risk": self._risk(patient_id, score_risk), "record": self._record(patient_id),
        }

    def _risk(self, patient_id: str, fresh: bool) -> list[dict]:
        if not self.serving_url:
            return []
        try:
            if fresh:
                r = httpx.post(f"{self.serving_url}/v1/patients/{patient_id}/risk", timeout=10)
            else:
                r = httpx.get(f"{self.serving_url}/v1/patients/{patient_id}/risk", timeout=5)
            r.raise_for_status()
            return [{k: m.get(k) for k in ("model", "version", "outcome", "probability", "band", "validated",
                                           "metrics", "top_factors", "imputed")} for m in r.json()]
        except httpx.HTTPError as exc:
            log.warning("risk scoring unavailable", extra={"patient_id": patient_id, "error": str(exc)[:200]})
            return []

    def _record(self, patient_id: str) -> dict:
        """Problem list + recent medication orders from FHIR (the clinical system of record)."""
        if not self.fhir_base:
            return {}
        out: dict = {"conditions": [], "medications": []}
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
            log.warning("FHIR context unavailable", extra={"patient_id": patient_id, "error": str(exc)[:200]})
        return out


def render(ctx: dict, event: dict) -> str:
    p = ctx["profile"]
    st = ctx["status"] or {}
    latest = {k: v.get("v") for k, v in (st.get("latest") or {}).items() if isinstance(v, dict)}
    blocks = [
        f"[EVENT] {_j({k: event.get(k) for k in ('type', 'severity', 'summary', 'occurred_at', 'evidence')})}",
        f"[PROFILE] {_j({k: p[k] for k in p if k != 'thresholds'})}",
        f"[CURRENT] latest={_j(latest)} news2={st.get('news2')} band={st.get('news2_band')} "
        f"partial={st.get('news2_partial')} detail={_j(st.get('news2_detail'))} device={st.get('device_id')} "
        f"last_seen={st.get('last_seen')}",
        "[TREND_6H] 30-minute buckets: " + _j(ctx["trend"]),
        "[RECENT_WINDOWS] oldest→newest: " + _j(ctx["recent"]),
        "[EVENTS_24H] newest first: " + _j(ctx["events"]),
        f"[ACTIVE_ALERT] {_j(ctx['alert'])}",
        "[RISK_MODELS] " + _j(ctx["risk"]),
        f"[RECORD] {_j(ctx['record'])}",
        "[RECENT_AGENT_ACTIONS] last 2 h: " + _j(ctx["recent_actions"]),
    ]
    return "\n".join(blocks)
