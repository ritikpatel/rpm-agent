"""Claude access for the agents.

Providers: `bedrock` (production; AWS credentials via EKS Pod Identity), `anthropic` (direct API for development),
`offline` (no model; deterministic rules and templates, also used when a call fails). Every request uses adaptive
thinking with an explicit effort, an explicitly cached stable system prompt, and a refusal fallback (server-side
`fallbacks: "default"` on the Claude API, client-side middleware on Bedrock).

JSON output: the Claude API uses structured outputs (`output_config.format`). Claude in Amazon Bedrock does not
support structured outputs, so there the answer is returned through a single tool call (`tool_choice: auto` plus an
instruction, since forced tool choice cannot be combined with thinking) and validated by the caller's schema.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


class LLMUnavailable(Exception):
    """Call failed or was refused; callers degrade to the offline path."""


@dataclass
class LLMResult:
    data: dict
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    latency_ms: int


class ClaudeClient:
    def __init__(self, provider: str, model: str = "claude-opus-5", region: str = "ap-southeast-2",
                 bedrock_fallback_model: str = "claude-opus-4-8", timeout_s: float = 90):
        import anthropic

        self._anthropic = anthropic
        self.provider = provider
        if provider == "bedrock":
            def prefixed(m: str) -> str:
                # bare names get the provider prefix; full ids, including inference profiles such as
                # "au.anthropic.claude-opus-5", pass through unchanged
                return m if "anthropic." in m else f"anthropic.{m}"

            self.model = prefixed(model)
            self.client = anthropic.AnthropicBedrockMantle(
                aws_region=region, timeout=timeout_s,
                middleware=[anthropic.BetaRefusalFallbackMiddleware([{"model": prefixed(bedrock_fallback_model)}])])
            self.extra: dict = {}
        elif provider == "anthropic":
            self.model = model
            self.client = anthropic.Anthropic(timeout=timeout_s)
            self.extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        else:
            raise ValueError(f"unsupported provider {provider}")

    @staticmethod
    def _extract(resp, tool_name: str) -> dict:
        return _extract_json(resp, tool_name)

    def complete_json(self, *, system: str, user: str, schema: dict, effort: str = "medium",
                      max_tokens: int = 16000, tool_name: str = "submit_result") -> LLMResult:
        a = self._anthropic
        t0 = time.monotonic()
        request: dict = {
            "model": self.model, "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "thinking": {"type": "adaptive"},
            **self.extra,
        }
        if self.provider == "bedrock":
            request["tools"] = [{"name": tool_name, "description": "Record the final answer.", "input_schema": schema}]
            request["tool_choice"] = {"type": "auto"}
            request["output_config"] = {"effort": effort}
            request["messages"] = [{"role": "user", "content": f"{user}\n\nRecord your answer by calling the "
                                                                   f"`{tool_name}` tool exactly once."}]
        else:
            request["output_config"] = {"effort": effort, "format": {"type": "json_schema", "schema": schema}}
            request["messages"] = [{"role": "user", "content": user}]
        try:
            resp = self.client.beta.messages.create(**request)
        except a.RateLimitError as exc:
            raise LLMUnavailable(f"rate limited: {exc}") from exc
        except a.APIStatusError as exc:
            raise LLMUnavailable(f"API error {exc.status_code}: {str(exc)[:200]}") from exc
        except a.APIConnectionError as exc:
            raise LLMUnavailable(f"connection error: {exc}") from exc
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            raise LLMUnavailable(f"refused ({getattr(details, 'category', None)})")
        if resp.stop_reason == "max_tokens":
            raise LLMUnavailable("output truncated at max_tokens")
        data = self._extract(resp, tool_name)
        u = resp.usage
        return LLMResult(data, getattr(resp, "model", self.model), self.provider, u.input_tokens, u.output_tokens,
                         getattr(u, "cache_read_input_tokens", 0) or 0, int((time.monotonic() - t0) * 1000))


def _extract_json(resp, tool_name: str) -> dict:
    """Structured output arrives as the text block (Claude API) or as the tool call's input (Bedrock)."""
    tool = next((b for b in resp.content if b.type == "tool_use" and b.name == tool_name), None)
    if tool is not None:
        return tool.input if isinstance(tool.input, dict) else json.loads(tool.input)
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        raise LLMUnavailable("no answer in response")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMUnavailable("answer was not valid JSON") from exc


def make_client(provider: str, model: str, region: str) -> ClaudeClient | None:
    if provider == "offline":
        return None
    try:
        return ClaudeClient(provider, model, region)
    except Exception as exc:  # noqa: BLE001 - missing credentials must not stop the safety net
        log.warning("LLM provider unavailable; running offline", extra={"provider": provider, "error": str(exc)})
        return None
