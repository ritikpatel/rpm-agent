"""ClaudeClient request shaping and response handling, with the SDK client replaced by a fake."""

from types import SimpleNamespace as NS

import pytest

from agent_service.llm import ClaudeClient, LLMUnavailable
from agent_service.schemas import ASSESSMENT_SCHEMA


class FakeMessages:
    def __init__(self, response):
        self.response, self.calls = response, []

    def create(self, **kw):
        self.calls.append(kw)
        return self.response


def client(provider, response):
    c = ClaudeClient.__new__(ClaudeClient)
    import anthropic
    c._anthropic, c.provider = anthropic, provider
    c.model = "anthropic.claude-opus-5" if provider == "bedrock" else "claude-opus-5"
    c.extra = {} if provider == "bedrock" else {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    fake = FakeMessages(response)
    c.client = NS(beta=NS(messages=fake))
    return c, fake


USAGE = NS(input_tokens=1200, output_tokens=300, cache_read_input_tokens=1000)
ANSWER = {"signal_quality": "reliable", "concern_level": "high", "clinical_picture": "x", "key_findings": [],
          "recommended_actions": [], "model_evidence_note": "", "confidence": "medium"}


def test_claude_api_uses_structured_outputs_and_default_fallbacks():
    import json
    resp = NS(stop_reason="end_turn", model="claude-opus-5", usage=USAGE,
              content=[NS(type="thinking", thinking=""), NS(type="text", text=json.dumps(ANSWER))])
    c, fake = client("anthropic", resp)
    res = c.complete_json(system="S", user="U", schema=ASSESSMENT_SCHEMA, effort="medium")
    req = fake.calls[0]
    assert req["output_config"]["format"]["type"] == "json_schema" and req["output_config"]["effort"] == "medium"
    assert req["fallbacks"] == "default" and req["thinking"] == {"type": "adaptive"}
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"} and "tools" not in req
    assert res.data == ANSWER and res.cache_read_tokens == 1000


def test_bedrock_returns_answer_through_tool_call():
    resp = NS(stop_reason="tool_use", model="anthropic.claude-opus-5", usage=USAGE,
              content=[NS(type="tool_use", name="submit_result", input=ANSWER)])
    c, fake = client("bedrock", resp)
    res = c.complete_json(system="S", user="U", schema=ASSESSMENT_SCHEMA)
    req = fake.calls[0]
    assert "format" not in req["output_config"] and req["tool_choice"] == {"type": "auto"}
    assert req["tools"][0]["input_schema"] == ASSESSMENT_SCHEMA and "fallbacks" not in req
    assert res.data == ANSWER


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_refusal_and_truncation_degrade(stop):
    resp = NS(stop_reason=stop, model="m", usage=USAGE, content=[], stop_details=NS(category="bio"))
    c, _ = client("anthropic", resp)
    with pytest.raises(LLMUnavailable):
        c.complete_json(system="S", user="U", schema=ASSESSMENT_SCHEMA)
