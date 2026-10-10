from datetime import UTC, datetime, timedelta

from agent_service.graph import burst_lead, check_citations, classify
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


def test_burst_runs_once_on_most_severe_event():
    def ev(eid, etype, sev, alert):
        return {"event_id": eid, "type": etype, "severity": sev, "requires_alert": alert}
    rising = ev("1", "NEWS2_RISING", "low", False)
    edge = ev("2", "EDGE_ALERT", "high", True)
    crit = ev("3", "SPO2_CRITICAL", "critical", True)
    reply = ev("4", "CHECKIN_REPLY", "critical", False)
    assert burst_lead(rising, [rising, edge, crit, reply])["event_id"] == "3"  # a reply is never a lead
    assert burst_lead(edge, [rising, edge])["event_id"] == "2"
    twin = ev("5", "SPO2_LOW", "high", True)
    assert burst_lead(edge, [twin, edge])["event_id"] == "2"  # ties keep the triggering event


def test_offline_recommendation_matches_the_event_not_the_first_protocol_line():
    from agent_service.offline import recommendation_line

    news2 = ("- Aggregate 0-4: routine monitoring.\n- Any single parameter scoring 3 (red score): urgent review.\n"
             "- Aggregate 5-6: urgent response.\n- Aggregate 7 or more: emergency response.")
    ev = lambda total, **evidence: {"severity": "high", "news2": {"total": total}, "evidence": evidence}  # noqa: E731
    assert recommendation_line(news2, ev(9)).startswith("Aggregate 7 or more")
    assert recommendation_line(news2, ev(5)).startswith("Aggregate 5-6")
    assert recommendation_line(news2, ev(3, red_params=["sbp"])).startswith("Any single parameter")
    assert recommendation_line(news2, ev(2)).startswith("Aggregate 0-4")
    hypoxia = "- Below 92 %: review within 30 minutes.\n- Below 90 %: urgent review.\n- Below 85 %: emergency response."
    spo2 = lambda v, sev: {"severity": sev, "evidence": {"spo2": v}}  # noqa: E731
    assert recommendation_line(hypoxia, spo2(83, "critical")).startswith("Below 85")
    assert recommendation_line(hypoxia, spo2(89, "medium")).startswith("Below 90")
    assert recommendation_line("- Call the patient.\n- Immediate ambulance if unresponsive.",
                               {"severity": "critical"}).startswith("Immediate")
