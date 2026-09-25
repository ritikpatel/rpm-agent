# ADR 0007 — Agent workflow, model and provider choices

**Status:** accepted · 2026-09-25

## Decision
- **Workflow:** LangGraph state graph (`services/agent-service/graph.py`):
  load_context → assess → gate → act → retrieve → brief → persist, with separate branches for patient replies and
  informational events. Nodes call Claude through the official Anthropic SDK; no LangChain model wrappers.
- **Model:** `claude-opus-5` for both LLM steps, cost-tuned with `output_config.effort` (`medium` by default)
  rather than by switching to a smaller model. Model and effort are configuration (`RPM_LLM_MODEL`,
  `RPM_LLM_EFFORT_*`). *(Changed from the plan's Sonnet/Haiku split, following current Claude API guidance: measure
  the most capable model at a lower effort before building a multi-model cascade.)*
- **Providers:** `bedrock` (production: PHI stays inside the AWS boundary; Pod Identity credentials), `anthropic`
  (development), `offline` (deterministic rules and templates, no model).
  - Claude API: structured outputs (`output_config.format`) plus server-side `fallbacks: "default"` for refusals.
  - Claude in Amazon Bedrock: structured outputs are **not** supported on that endpoint. The answer comes back
    through one tool call (`tool_choice: auto`, since forced tool choice cannot be combined with thinking) and is
    validated with Pydantic. Refusals are handled by the SDK's client-side `BetaRefusalFallbackMiddleware`
    (fallback model Opus 4.8). Prompt caching uses an explicit breakpoint on the static system prompt.
- **Graceful degradation:** any LLM failure, refusal, truncation or schema violation falls back to the offline path
  for that node, and the run is recorded as `degraded`. Rule-level alerts never depend on this service (ADR 0001).
- **Guardrails outside the model:**
  - The policy gate can raise severity but never lower it, and blocks escalation on suspected artefact.
  - Patient-facing actions are limited to home patients, repeats within 15 minutes are blocked, and each run may
    send at most three device commands.
  - Patient-facing wording comes from a fixed, clinician-approved catalogue and is never generated.
  - Every citation in a brief is checked against the retrieved sources.
- **Cost control:** one run per patient at a time (Redis lock). Repeat events of the same or lower severity within
  120 s are debounced. Informational events never reach the model.

## Consequences
The whole platform runs without any API key (offline mode), so tests and CI are deterministic. The two LLM paths
are covered by request-shaping unit tests with a fake SDK client. A live model run needs Bedrock model access (or
an Anthropic key), which is a prerequisite on the AWS account.
