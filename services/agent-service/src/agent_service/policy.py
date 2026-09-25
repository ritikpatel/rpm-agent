"""Deterministic guardrails between the model's recommendations and the real world (ADR 0001)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from rpm_alerts import SEVERITY_RANK

PATIENT_FACING = {"REQUEST_SPOT_CHECK", "SEND_CHECKIN", "REQUEST_HOME_READINGS"}
DEVICE_ACTIONS = PATIENT_FACING | {"SET_PROFILE_ELEVATED", "SET_PROFILE_CRITICAL"}
REPEAT_WINDOW = timedelta(minutes=15)
MAX_DEVICE_ACTIONS_PER_RUN = 3

# Clinician-approved patient wording (never model-generated).
CHECKIN_QUESTIONS = {
    "breathless": "Are you more short of breath than usual right now?",
    "dizzy": "Do you feel dizzy, faint, confused, or is your heart racing?",
    "fever": "Do you feel feverish, shivery or have chills?",
    "chest_pain": "Do you have chest pain, pressure or tightness right now?",
    "readings": "Please measure your blood pressure and temperature now and enter them in the app. Done?",
}


def checkin_topic(event_type: str, condition: str | None, latest: dict) -> str:
    if event_type in ("HR_SUSTAINED_HIGH", "HR_SUSTAINED_LOW"):
        return "dizzy"
    if condition == "IHD":
        return "chest_pain"
    if event_type.startswith("SPO2") or condition in ("COPD", "CHF"):
        return "breathless"
    if condition == "Sepsis" or (latest.get("temp") or 0) >= 38:
        return "fever"
    return "breathless"


@dataclass
class Decision:
    allowed: list[dict] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    raise_severity_to: str | None = None
    disagreement: str | None = None


def gate(assessment: dict, *, care_setting: str, rule_severity: str, alert: dict | None,
         recent_actions: list[dict], now: datetime) -> Decision:
    d = Decision()
    concern = assessment["concern_level"]
    artifact = assessment["signal_quality"] == "possible_artifact"
    recent = {a["action"] for a in recent_actions
              if a.get("at") and now - datetime.fromisoformat(a["at"]) < REPEAT_WINDOW}
    device_count = 0
    for rec in assessment.get("recommended_actions", []):
        action, why = rec["action"], None
        if action == "NONE":
            continue
        if care_setting != "home" and action in PATIENT_FACING:
            why = "patient-facing actions apply to home patients only"
        elif action in recent:
            why = "same action already taken in the last 15 minutes"
        elif artifact and action not in ("REQUEST_SPOT_CHECK", "SET_PROFILE_ELEVATED"):
            why = "signal may be artefact: only verification actions allowed"
        elif action == "ESCALATE" and (SEVERITY_RANK.get(concern, 0) < SEVERITY_RANK["high"] or alert is None):
            why = "escalation requires high/critical concern and an active alert"
        elif action in DEVICE_ACTIONS and device_count >= MAX_DEVICE_ACTIONS_PER_RUN:
            why = "device action budget for this run exhausted"
        if why:
            d.blocked.append({**rec, "blocked_reason": why})
            continue
        device_count += action in DEVICE_ACTIONS
        d.allowed.append(rec)

    concern_rank = SEVERITY_RANK.get(concern, 0)
    current = alert["severity"] if alert else rule_severity
    if not artifact and concern_rank > SEVERITY_RANK.get(current, 0) and alert is not None:
        d.raise_severity_to = concern  # agents may escalate ...
    elif concern_rank < SEVERITY_RANK.get(current, 0):
        d.disagreement = (f"Agent judges concern '{concern}' below alert severity '{current}'; severity unchanged "
                          "(only a clinician can downgrade).")  # ... but never de-escalate
    return d
