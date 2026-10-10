"""The four-agent workflow against a fake care-api, with and without a model."""

from datetime import UTC, datetime

from agent_service.graph import AgentGraph, Deps
from agent_service.llm import SUBMIT
from rpm_core.contracts import AgentTask
from rpm_core.rag import Hit

from .test_llm_client import FakeConverse, reply, use

NOW = datetime(2026, 10, 2, 10, 0, tzinfo=UTC).isoformat()
EVENT = {"event_id": "0192aaaa-0000-7000-8000-000000000001", "patient_id": "p-1", "type": "SPO2_LOW",
         "severity": "medium", "requires_alert": True, "occurred_at": NOW, "summary": "SpO2 88% sustained",
         "evidence": {"spo2": 88}}


class FakeApi:
    def __init__(self, setting="home", alert_severity="medium"):
        self.calls, self.runs, self.briefs, self.raised = [], [], [], []
        self.setting, self.alert = setting, {"alert_id": "a-1", "severity": alert_severity, "status": "open"}

    def section(self, patient_id, name, agent, fresh=False):
        self.calls.append((agent, name))
        return {
            "profile": {"profile": {"patient_id": patient_id, "display_name": "Test Patient", "age": 70, "sex": "F",
                                    "primary_condition": "COPD", "care_setting": self.setting}},
            "vitals": {"current": {"news2": 5, "news2_band": "medium", "news2_partial": True, "latest": {"spo2": 88}},
                       "trend_6h": [{"hr_avg": 80, "spo2_min": 94, "news2_max": 1},
                                    {"hr_avg": 96, "spo2_min": 88, "news2_max": 5}],
                       "recent_windows": [{"hr": 96, "hr_q": 0.9, "spo2": 88}]},
            "events": {"events_24h": [], "active_alert": self.alert, "recent_agent_actions": [], "messages_24h": []},
            "risk": {"risk_models": [{"model": "early_deterioration", "probability": 0.41, "band": "medium",
                                      "validated": False}]},
            "record": {"conditions": ["COPD"], "medications": [], "labs": []},
        }[name]

    def search(self, agent, query, patient_id=None, kind=None, k=6, with_actions=False):
        self.calls.append((agent, f"search:{kind}"))
        if kind == "discharge_summary":
            return [Hit("DS-p-1#1", "DS-p-1", "discharge_summary", "Discharge summary", "Admission", "COPD", 0.0)]
        return [Hit("P-HYPOXIA#2", "P-HYPOXIA", "protocol", "Hypoxia", "Actions",
                    "- Below 90 %: urgent review within 15 minutes.", 0.0)]

    def timeline(self, alert_id, agent, kind, summary, details=None):
        self.calls.append((agent, f"timeline:{kind}"))

    def raise_severity(self, alert_id, agent, severity, reason):
        self.raised.append(severity)
        return True

    def escalate(self, alert_id, agent, reason):
        self.calls.append((agent, "escalate"))

    def device(self, patient_id, agent, cmd, args):
        self.calls.append((agent, f"device:{cmd}"))
        return {"cmd_id": "c-1", "device_id": "sim-1"}

    def brief(self, alert_id, agent, brief, status, assessment):
        self.briefs.append((alert_id, status, brief))

    def open_alert(self, agent, event):
        self.calls.append((agent, "open_alert"))
        return "a-2"

    def save_run(self, run):
        self.runs.append(run)


def task(trigger="alert", event=EVENT, alert_id="a-1"):
    return AgentTask(trigger=trigger, patient_id="p-1", alert_id=alert_id, event=event)


def test_without_a_model_the_run_degrades_to_rules_and_still_briefs():
    api = FakeApi()
    out = AgentGraph(Deps(api=api, llm=None)).run(task())
    assert out["assessment_by"] == "offline-rules" and out["brief_status"] == "offline"
    assert api.briefs[0][0] == "a-1" and "[P-HYPOXIA#2]" in api.briefs[0][2]["recommendation"]
    run = api.runs[0]
    assert run["status"] == "degraded" and run["provider"] == "offline" and run["alert_id"] == "a-1"
    assert ("escalation", "device:CHECKIN") in api.calls   # the rules still ask the home patient how they feel


def test_four_agents_read_through_their_own_tools_and_the_gate_applies():
    from agent_service.llm import BedrockLLM

    fake = FakeConverse(
        reply(use("get_vitals", {})),
        reply(use(SUBMIT, {"signal_quality": "reliable", "clinical_picture": "SpO2 falling to 88%.",
                           "key_findings": [{"finding": "SpO2 88%", "evidence": "latest spo2 88"}]})),
        reply(use("get_risk", {})),
        reply(use(SUBMIT, {"concern_level": "high", "confidence": "medium", "risk_summary": "COPD, falling SpO2.",
                           "model_evidence_note": "unvalidated model 0.41", "key_findings": []})),
        reply(use("search_guidelines", {"query": "hypoxia escalation", "kind": "protocol"})),
        reply(use(SUBMIT, {"recommended_actions": [{"action": "REQUEST_SPOT_CHECK", "reason": "confirm"},
                                                   {"action": "ESCALATE", "reason": "sustained hypoxia"}],
                           "rationale": "Hypoxia protocol"})),
        reply(use(SUBMIT, {"headline": "Hypoxia in COPD patient", "situation": "SpO2 88% [EVENT]",
                           "background": "COPD [DS-p-1#1]", "assessment": "Falling SpO2 [MONITORING]",
                           "recommendation": "Urgent review within 15 minutes [P-HYPOXIA#2]",
                           "citations": [{"ref": "P-HYPOXIA#2", "supports": "review timing"}],
                           "uncertainties": ["Partial NEWS2"]})))
    api = FakeApi()
    out = AgentGraph(Deps(api=api, llm=BedrockLLM(client=fake), model_fast="fast", model_reasoning="smart")).run(task())
    assert out["assessment_by"] == "agents" and out["assessment"]["concern_level"] == "high"
    assert api.raised == ["high"]                                    # agents may raise severity
    assert [a["action"] for a in out["actions"]] == ["REQUEST_SPOT_CHECK", "ESCALATE"]
    assert out["brief"]["citation_check"] == "passed" and api.briefs[0][1] == "ready"
    assert ("monitoring", "vitals") in api.calls and ("risk", "risk") in api.calls
    assert [c["modelId"] for c in fake.calls] == ["fast", "fast", "fast", "fast", "smart", "smart", "smart"]
    run = api.runs[0]
    assert run["status"] == "succeeded" and run["input_tokens"] == 700
    assert set(run["trace"]["agents"]) == {"monitoring", "risk", "escalation"}


def test_agents_cannot_lower_severity_and_icu_blocks_patient_actions():
    from agent_service.llm import BedrockLLM

    fake = FakeConverse(
        reply(use(SUBMIT, {"signal_quality": "reliable", "clinical_picture": "Stable.", "key_findings": []})),
        reply(use(SUBMIT, {"concern_level": "low", "confidence": "medium", "risk_summary": "Low.",
                           "model_evidence_note": "", "key_findings": []})),
        reply(use(SUBMIT, {"recommended_actions": [{"action": "SEND_CHECKIN", "reason": "ask"}], "rationale": "x"})),
        reply(use(SUBMIT, {"headline": "h", "situation": "s", "background": "b", "assessment": "a",
                           "recommendation": "Review [P-HYPOXIA#2]", "citations": [], "uncertainties": []})))
    api = FakeApi(setting="icu", alert_severity="high")
    out = AgentGraph(Deps(api=api, llm=BedrockLLM(client=fake))).run(task())
    assert api.raised == [] and "only a clinician can downgrade" in out["decision"]["disagreement"]
    assert out["actions"] == [] and out["decision"]["blocked"][0]["action"] == "SEND_CHECKIN"


def test_symptom_reply_raises_the_alert_without_any_model_call():
    api = FakeApi()
    ev = {**EVENT, "type": "CHECKIN_REPLY", "requires_alert": False, "severity": "info",
          "evidence": {"answer": "Yes", "question_id": "breathless-1"}}
    out = AgentGraph(Deps(api=api, llm=None)).run(task("reply", ev, None))
    assert api.raised == ["high"] and out["actions"][0]["action"] == "PATIENT_REPLY"
    assert api.runs[0]["trigger"] == "reply" and api.runs[0]["status"] == "succeeded"


def test_review_without_an_event_or_alert_still_produces_a_summary():
    api = FakeApi()
    api.alert = None
    out = AgentGraph(Deps(api=api, llm=None)).run(AgentTask(trigger="on_demand", patient_id="p-1"))
    assert out["brief"] and not api.briefs and api.runs[0]["brief"]
