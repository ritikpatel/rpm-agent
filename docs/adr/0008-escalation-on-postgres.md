# ADR 0008 — Escalation timers and notifications on Postgres (instead of Step Functions)

**Status:** accepted · 2026-09-25 · *supersedes the plan's "Step Functions timers"*

## Context
The plan proposed one Step Functions execution per alert (wait for acknowledgement, then escalate). Building it
exposed three problems: no local equivalent (breaking ADR 0003's parity), two sources of truth for alert state, and
awkward exactly-once handling between SQS, Postgres and the state machine.

## Decision
- The alert state machine lives in Postgres (`libs/rpm_alerts`).
- **Exactly-once intake:** `consumed_events` is written in the same transaction as the alert change.
- **One active alert per patient:** a partial unique index enforces it; new events attach to that alert and can
  only raise its severity. Worsening re-pages even after acknowledgement.
- **Timers:** the `next_escalation_at` column is read with `SELECT … FOR UPDATE SKIP LOCKED` every second, which
  is safe with several replicas.
- **Transactional outbox:** `notify_pending_tier` is set together with the state change. The dispatcher sends
  through every channel while holding the row lock, records one `notifications` row per channel (with sensor →
  hand-off latency), and clears the flag in the same transaction. A crash cannot lose a page. At worst a page is
  repeated.

## Consequences
Behaviour is identical locally and on AWS, and is covered by integration tests against real Postgres. There is no
visual execution graph in the AWS console; the alert timeline in the clinician console serves that purpose.
Step Functions remains an option if a separate workflow team needs it.
