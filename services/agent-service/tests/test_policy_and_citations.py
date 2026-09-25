from datetime import UTC, datetime, timedelta

from agent_service.graph import check_citations, classify
from agent_service.policy import gate
from agent_service.schemas import ASSESSMENT_SCHEMA, Assessment
from rpm_rag import Hit

NOW = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)


def assessment(concern="high", quality="reliable", actions=()):
    return {"signal_quality": quality, "concern_level": concern, "clinical_picture": "x", "key_findings": [],
            "recommended_actions": [{"action": a, "reason": "r"} for a in actions], "model_evidence_note": "",
            "confidence": "medium"}


ALERT = {"alert_id": "a1", "severity": "medium"}


def test_agent_can_raise_but_never_lower_severity():
    d = gate(assessment("critical"), care_setting="home", rule_severity="medium", alert=ALERT, recent_actions=[],
             now=NOW)
    assert d.raise_severity_to == "critical" and d.disagreement is None
    d = gate(assessment("low"), care_setting="home", rule_severity="medium", alert=ALERT, recent_actions=[], now=NOW)
    assert d.raise_severity_to is None and "only a clinician can downgrade" in d.disagreement


def test_artifact_limits_actions_to_verification():
    d = gate(assessment("high", "possible_artifact", ["REQUEST_SPOT_CHECK", "ESCALATE", "SEND_CHECKIN"]),
             care_setting="home", rule_severity="medium", alert=ALERT, recent_actions=[], now=NOW)
    assert [a["action"] for a in d.allowed] == ["REQUEST_SPOT_CHECK"]
    assert d.raise_severity_to is None  # never escalate on suspect data


def test_patient_facing_actions_blocked_in_icu_and_repeats_blocked():
    recent = [{"action": "SET_PROFILE_ELEVATED", "at": (NOW - timedelta(minutes=5)).isoformat()}]
    d = gate(assessment("high", actions=["SEND_CHECKIN", "SET_PROFILE_ELEVATED", "ESCALATE"]), care_setting="icu",
             rule_severity="high", alert={"alert_id": "a", "severity": "high"}, recent_actions=recent, now=NOW)
    assert [a["action"] for a in d.allowed] == ["ESCALATE"]
    assert {b["action"] for b in d.blocked} == {"SEND_CHECKIN", "SET_PROFILE_ELEVATED"}


def test_escalate_requires_high_concern_and_alert():
    d = gate(assessment("medium", actions=["ESCALATE"]), care_setting="home", rule_severity="medium", alert=ALERT,
             recent_actions=[], now=NOW)
    assert not d.allowed and d.blocked[0]["action"] == "ESCALATE"


def test_citation_check():
    hits = [Hit("P-SEPSIS#3", "P-SEPSIS", "protocol", "Sepsis", "Actions", "…", 1.0),
            Hit("DS-p-1#1", "DS-p-1", "discharge_summary", "DS", "Admission", "…", 1.0)]
    good = {"situation": "NEWS2 8 [CURRENT]", "background": "CHF [DS-p-1#1]", "assessment": "rising [TREND_6H]",
            "recommendation": "Escalate now [P-SEPSIS#3]", "citations": [{"ref": "P-SEPSIS#3", "supports": "x"}]}
    assert check_citations(good, hits) == (True, [])
    bad = {**good, "recommendation": "Give antibiotics [P-MADEUP#1]"}
    ok, problems = check_citations(bad, hits)
    assert not ok and "unknown reference P-MADEUP#1" in problems and "recommendation cites no protocol" in problems


def test_trigger_classification():
    assert classify({"type": "CHECKIN_REPLY", "requires_alert": False}) == "reply"
    assert classify({"type": "SPO2_LOW", "requires_alert": True}) == "alert"
    assert classify({"type": "NEWS2_RISING", "requires_alert": False}) == "monitor"
    assert classify({"type": "DATA_GAP", "requires_alert": False}) == "record"


def test_assessment_schema_is_strict_and_matches_model():
    def strict(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False and set(node["required"]) == set(node["properties"])
            for child in node["properties"].values():
                strict(child)
        if node.get("type") == "array":
            strict(node["items"])
    strict(ASSESSMENT_SCHEMA)
    Assessment.model_validate(assessment(actions=["NONE"]))
