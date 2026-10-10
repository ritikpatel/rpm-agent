"""The Bedrock tool loop, with the Converse client replaced by a scripted fake."""

import pytest

from agent_service.llm import SUBMIT, BedrockLLM, LLMUnavailable, Tool

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


def use(name, payload, tid="t1"):
    return {"toolUse": {"toolUseId": tid, "name": name, "input": payload}}


def reply(*blocks, stop="tool_use", tokens=(100, 20)):
    return {"output": {"message": {"role": "assistant", "content": list(blocks)}}, "stopReason": stop,
            "usage": {"inputTokens": tokens[0], "outputTokens": tokens[1]}}


class FakeConverse:
    def __init__(self, *responses, fail_models=()):
        self.responses, self.calls, self.fail_models = list(responses), [], set(fail_models)

    def converse(self, **kw):
        self.calls.append(kw)
        if kw["modelId"] in self.fail_models:
            raise RuntimeError("ThrottlingException")
        return self.responses.pop(0)


def run(fake, tools=(), **kw):
    return BedrockLLM(client=fake, **kw).run(model="m1", system="s", user="u", tools=list(tools),
                                            output_schema=SCHEMA, max_turns=3)


def test_tool_is_called_then_result_is_submitted():
    seen = []
    tool = Tool("get_vitals", "d", {"type": "object", "properties": {}}, lambda a: seen.append(a) or {"hr": 118})
    fake = FakeConverse(reply(use("get_vitals", {})), reply(use(SUBMIT, {"answer": "tachycardia"}, "t2")))
    out = run(fake, [tool])
    assert out.data == {"answer": "tachycardia"} and out.turns == 2 and seen == [{}]
    assert out.input_tokens == 200 and out.tool_calls == [{"tool": "get_vitals", "input": {}}]
    result = fake.calls[1]["messages"][-1]["content"][0]["toolResult"]
    assert result["toolUseId"] == "t1" and '"hr": 118' in result["content"][0]["text"]
    assert [t["toolSpec"]["name"] for t in fake.calls[0]["toolConfig"]["tools"]] == ["get_vitals", SUBMIT]
    assert fake.calls[0]["inferenceConfig"]["maxTokens"] > 0   # always set explicitly


def test_failing_tool_is_reported_to_the_model_not_raised():
    def boom(_):
        raise RuntimeError("care-api down")
    fake = FakeConverse(reply(use("get_vitals", {})), reply(use(SUBMIT, {"answer": "no data"}, "t2")))
    out = run(fake, [Tool("get_vitals", "d", {"type": "object"}, boom)])
    assert out.data["answer"] == "no data"
    assert fake.calls[1]["messages"][-1]["content"][0]["toolResult"]["status"] == "error"


def test_last_turn_forces_submission():
    text = {"text": "thinking"}
    fake = FakeConverse(reply(text, stop="end_turn"), reply(text, stop="end_turn"),
                        reply(use(SUBMIT, {"answer": "done"})))
    assert run(fake).data == {"answer": "done"}
    assert "toolChoice" not in fake.calls[0]["toolConfig"]
    assert fake.calls[2]["toolConfig"]["toolChoice"] == {"tool": {"name": SUBMIT}}


def test_fallback_model_is_used_when_the_first_fails():
    fake = FakeConverse(reply(use(SUBMIT, {"answer": "ok"})), fail_models={"m1"})
    out = run(fake, fallback_model="m2")
    assert out.model == "m2" and [c["modelId"] for c in fake.calls] == ["m1", "m2"]


@pytest.mark.parametrize("fake", [
    FakeConverse(fail_models={"m1"}),
    FakeConverse(reply({"text": "cut"}, stop="max_tokens")),
    FakeConverse(reply({"text": "a"}, stop="end_turn"), reply({"text": "b"}, stop="end_turn"),
                 reply({"text": "c"}, stop="end_turn")),
])
def test_failures_degrade_instead_of_raising_provider_errors(fake):
    with pytest.raises(LLMUnavailable):
        run(fake)
