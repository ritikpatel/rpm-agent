# Runbook

Operations for the local stack and the AWS demo environment: day-to-day checks and incident procedures. Commands
assume the repository root.

## Start and stop

| | Local | AWS |
|---|---|---|
| Up | `make up && RPM_DEMO_PATIENT=auto make seed && RPM_DEMO_PATIENT=auto make sim` | `TFVARS=my.tfvars make cloud-up` (about 25 min from scratch) |
| Console | http://localhost:3000 (dev sign-in: pick a role) | `make cloud-board`, then http://localhost:3000 (Cognito sign-in) |
| Users | not needed (dev tokens) | `make cloud-user U=nurse.jane E=jane@example.org R=nurse` |
| Dashboards | `make obs`, then http://localhost:3001 | `OBSERVABILITY=1` at cloud-up; `kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3001:80` |
| Down | `make down` | `make cloud-down`. **Do this after every session**: the environment costs about US$10/day while up. |

## Daily checks (about 2 minutes)

1. **Grafana, "RPM · latency and alerting":**
   - Sensor→page p95 below 5 s, and end-to-end p95 below 3 s.
   - "Failed pages" is 0.
   - "ICU backlog" is 0.
2. **Kinesis consumer lag:** `rpm_kinesis_millis_behind_latest` is close to 0.
3. **Audit export:** the latest row in `audit_exports` is under 2 h old. The CronJob `audit-export` succeeded in its last run.
4. **Budget:** the AWS budget alarm e-mail is quiet.

## Incidents

### Alert storm (many pages in a short time)

1. Check whether it is real. On the console worklist, is it one patient or many? In Grafana, which rule or context dominates?
2. Many patients at once usually means a data problem, not a clinical one:
   - a bad firmware or config push;
   - clock skew (watch `rpm_clock_skew_total`);
   - replayed backfill.

   Backfilled windows are flagged `buffered` and should not page. Check `vitals.buffered` for the affected devices.
3. If one rule misfires, tighten it with a per-patient threshold override (device shadow / `patients.thresholds`) rather than disabling it globally.
4. Record the incident. Re-run `rpm-eval replay` with the change before rolling it out (see `docs/evaluation-report.md`).

### Device or gateway silent (`DEVICE_SILENT`)

1. The console shows the last-seen time. For a watch, the patient may simply not be wearing it (`on_wrist`). The check-in command asks them.
2. **ICU gateway:**
   - `curl <gateway>:8081/status` shows its view of each bed and its uplink state.
   - Its outbox holds data while the uplink is down (`rpm_gw_outbox_depth`), and everything is backfilled on reconnect.
3. **Real device, suspected compromise:**
   - Deactivate the certificate: `aws iot update-certificate --certificate-id <id> --new-status INACTIVE`.
   - End the assignment in `device_assignments` (set `active_to`).
   - Issue a new identity (`edge/android/provision.sh`).

### Stream lag or stream engine down

1. `kubectl -n rpm get pods` and `kubectl -n rpm logs deploy/stream-engine`.
2. Restart it if needed. Processing resumes from the Kinesis checkpoint in Postgres, with no loss or duplicates (chaos experiment `stream-engine-kill`).
3. Kinesis keeps records for 24 h; lag beyond that loses data.

### LLM or Bedrock unavailable

1. Nothing is required clinically. Rules still page (chaos experiment `agent-down`), and briefs arrive once the model is back, because SQS retains the events.
2. Check `rpm_agent_runs_total{status="degraded"}` and the agent-service logs. Throttling shows as degraded runs, which fall back to the offline template.

### Suspected unauthorised access

1. **Scope it.** Query `audit_log` or the exported AuditEvents: who read which patient, from which IP, with which token subject. Check CloudTrail for infrastructure changes.
2. **Contain it.** Disable the Cognito user (`aws cognito-idp admin-disable-user`) and revoke their tokens (`admin-user-global-sign-out`).
3. **Preserve evidence.** Audit objects are write-once (Object Lock), so do not delete the audit bucket.
4. **Notify.** Follow the breach-notification obligations in `docs/security/compliance-matrix.md` (HIPAA §164.404–410, GDPR Art. 33–34).

### Certificate rotation (devices)

1. Create a new certificate and attach it to the thing and policy. Provision the device (`provision.sh aws`).
2. Confirm it connects: IoT Core logs, or new rows in `vitals`.
3. Deactivate, then delete, the old certificate.

## Evaluation and verification

| What | Command | Needs |
|---|---|---|
| Unit and contract tests | `make test`, and `cd edge/android && ./gradlew :shared:test` | nothing |
| Alarm burden and sensitivity (offline replay) | `uv run rpm-eval replay --hours 24 --out eval/results/replay.json` | dataset |
| Chaos | `uv run rpm-eval chaos --out eval/results/chaos.json` | local stack plus simulator |
| Load | `uv run rpm-eval load --devices 1000` | local stack |
| Audit chain | query `audit_exports`; each `prev_sha256` equals the previous batch's `sha256` | database |
