"""Model access for the agents: Amazon Bedrock's Converse API, one bounded tool loop per agent.

An agent is given tools (which read through care-api) and one `submit_result` tool whose input schema is the
agent's output. The loop ends when the model submits; on the last allowed turn the model is forced to submit, so a
run always terminates. Any failure (throttling, access, malformed output) raises LLMUnavailable and the caller falls
back to deterministic rules: the agents advise, they are never a dependency of alerting.

Converse is used rather than a provider SDK so the same code runs Claude and Amazon Nova; the model is configuration.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

SUBMIT = "submit_result"


class LLMUnavailable(Exception):
    """The call failed or produced nothing usable; callers degrade to the offline path."""


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[[dict], object] | None = None   # None for the submit tool

    def spec(self) -> dict:
        return {"toolSpec": {"name": self.name, "description": self.description, "inputSchema": {"json": self.schema}}}


@dataclass
class AgentResult:
    data: dict
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    turns: int = 0
    tool_calls: list[dict] = field(default_factory=list)


class BedrockLLM:
    provider = "bedrock"

    def __init__(self, region: str = "ap-southeast-2", fallback_model: str | None = None, client=None,
                 timeout_s: float = 60):
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client("bedrock-runtime", region_name=region, config=Config(
                retries={"mode": "adaptive", "max_attempts": 4}, connect_timeout=5, read_timeout=timeout_s))
        self.client = client
        self.fallback_model = fallback_model

    def _converse(self, model: str, **kwargs) -> tuple[dict, str]:
        try:
            return self.client.converse(modelId=model, **kwargs), model
        except Exception as exc:  # noqa: BLE001 - botocore raises many types; all mean "this model did not answer"
            if self.fallback_model and self.fallback_model != model:
                log.warning("model call failed; trying the fallback model",
                            extra={"model": model, "fallback": self.fallback_model, "error": str(exc)[:200]})
                try:
                    return self.client.converse(modelId=self.fallback_model, **kwargs), self.fallback_model
                except Exception as exc2:  # noqa: BLE001
                    raise LLMUnavailable(f"{type(exc2).__name__}: {str(exc2)[:200]}") from exc2
            raise LLMUnavailable(f"{type(exc).__name__}: {str(exc)[:200]}") from exc

    def run(self, *, model: str, system: str, user: str, tools: list[Tool], output_schema: dict,
            max_turns: int = 5, max_tokens: int = 1800) -> AgentResult:
        submit = Tool(SUBMIT, "Submit your final answer. Call this exactly once, when you have what you need.",
                      output_schema)
        handlers = {t.name: t for t in tools}
        config = {"tools": [t.spec() for t in [*tools, submit]]}
        messages: list[dict] = [{"role": "user", "content": [{"text": user}]}]
        out = AgentResult(data={}, model=model)
        t0 = time.monotonic()
        for turn in range(max_turns):
            last = turn == max_turns - 1
            tool_config = {**config, "toolChoice": {"tool": {"name": SUBMIT}}} if last else config
            resp, used = self._converse(model, system=[{"text": system}], messages=messages, toolConfig=tool_config,
                                        inferenceConfig={"maxTokens": max_tokens})
            usage = resp.get("usage", {})
            out.model, out.turns = used, turn + 1
            out.input_tokens += usage.get("inputTokens", 0)
            out.output_tokens += usage.get("outputTokens", 0)
            message = resp["output"]["message"]
            calls = [b["toolUse"] for b in message.get("content", []) if "toolUse" in b]
            done = next((c for c in calls if c["name"] == SUBMIT), None)
            if done is not None:
                if not isinstance(done.get("input"), dict):
                    raise LLMUnavailable("submit_result input is not an object")
                out.data, out.latency_ms = done["input"], round((time.monotonic() - t0) * 1000)
                return out
            if resp.get("stopReason") == "max_tokens":
                raise LLMUnavailable("output truncated before the result was submitted")
            messages.append(message)
            if not calls:
                messages.append({"role": "user", "content": [{"text": f"Call {SUBMIT} with your answer now."}]})
                continue
            results = []
            for call in calls:
                tool = handlers.get(call["name"])
                record = {"tool": call["name"], "input": call.get("input")}
                try:
                    if tool is None or tool.handler is None:
                        raise KeyError(f"unknown tool {call['name']}")
                    payload = json.dumps(tool.handler(call.get("input") or {}), default=str)[:24000]
                    status = "success"
                except Exception as exc:  # noqa: BLE001 - the model sees the error and can carry on without the data
                    payload, status = f"tool failed: {str(exc)[:200]}", "error"
                    record["error"] = str(exc)[:200]
                out.tool_calls.append(record)
                results.append({"toolResult": {"toolUseId": call["toolUseId"], "content": [{"text": payload}],
                                               "status": status}})
            messages.append({"role": "user", "content": results})
        raise LLMUnavailable("no result submitted within the turn limit")


def make_llm(provider: str, region: str, fallback_model: str | None) -> BedrockLLM | None:
    if provider == "offline":
        return None
    if provider == "bedrock":
        return BedrockLLM(region, fallback_model)
    raise ValueError(f"unsupported RPM_LLM_PROVIDER {provider!r} (use bedrock or offline)")
