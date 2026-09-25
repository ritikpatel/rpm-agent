# ADR 0001 — Deterministic safety net before AI

**Status:** accepted · 2026-09-25

## Context
The platform uses LLM agents for triage, escalation and summaries. LLMs can be unavailable, slow or wrong, and a
missed deterioration is the worst failure mode of a monitoring system.

## Decision
- NEWS2 and threshold rules (`libs/rpm_features`) run in the stream engine on every window, with no model or LLM
  in the path.
- Any rule finding with `requires_alert=true` is sent to **two** queues: `rpm-clinical-events` (agents) and
  `rpm-alert-intake` (alert service). The alert path never waits on the agents.
- Agents may *escalate* (raise severity, add channels, request spot checks) but cannot *suppress* a rule-raised
  critical alert; only a clinician acknowledgement closes it.
- If Bedrock is unavailable the platform degrades to rule-based alerting and says so in the console.

## Consequences
Alert latency is bounded by the data plane (~0.5 s p95 locally), not the LLM. Agent value is measured as
*reduced alarm burden and better context*, not as detection.
