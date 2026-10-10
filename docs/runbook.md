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
2. **Ingest queue** (stream-engine metrics):
   - lag `rpm_ingest_queue_age_seconds` is close to 0;
   - `rpm_ingest_queue_depth{queue="dlq"}` is 0 (anything there was not processed);
   - the archiver's `rpm_archive_last_flush_timestamp_seconds` is under 2 minutes old while devices are sending.
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
2. Restart it if needed. Messages wait on the SQS queue `rpm-telemetry` and are processed on restart, with no loss or duplicates (chaos experiment `stream-engine-kill`).
   - The batch in flight at a hard kill returns after the queue's visibility timeout (60 s). Those windows are stored with `late = true` and are not applied to live state.
   - While catching up, the engine holds back half of each batch to restore order (`rpm_ingest_reorder_held`).
3. Run exactly one stream-engine replica. Per-device state is read-modify-write, so two consumers would race (ADR 0014).
4. The queue keeps messages for 4 days; lag beyond that loses data.

### Messages in a dead-letter queue

`rpm_ingest_queue_depth{queue="dlq"}` above 0, or messages in `rpm-telemetry-archive-dlq`.

1. A message lands there after 10 failed receives. Postgres or Redis being down does not cause this: the engine stops receiving until they answer.
2. Look at one message and at the engine log line `record failed; left on the queue` (it carries the `msg_id`):
   `aws sqs receive-message --queue-url <dlq-url> --visibility-timeout 0`.
3. After fixing the cause, move the messages back:
   `aws sqs start-message-move-task --source-arn <dlq-arn>`. Reprocessing is safe (`msg_id` dedupe).

### Raw archive behind or failing

1. `kubectl -n rpm logs deploy/telemetry-archiver`; check `rpm_archive_failures_total`.
2. Nothing is lost while it is down: messages wait on `rpm-telemetry-archive` for up to 4 days, and are deleted only after their S3 object is written. The real-time path uses a different queue and is unaffected.
3. After a crash the same records can be archived twice. Readers of the lake de-duplicate on `msg_id`.

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

## AWS plan limits (new AWS experience, Free plan)

The demo project is a "new AWS experience" project on the Free plan (US$100 of credits; the plan ends
2027-04-10). Its managed policies cannot be changed. Checked on 2026-10-10 with the project's own credentials;
re-check with `aws freetier get-account-plan-state --profile rpm-capstone` and the
[managed policies](https://docs.aws.amazon.com/accounts/latest/reference/scps-and-rcps-for-projects.html).

| Limit | What the platform does |
|---|---|
| Kinesis Data Streams and Amazon Data Firehose are Paid-plan services (explicit deny) | Device ingest runs on SQS with an in-cluster S3 archiver (ADR 0014) |
| One Region per project: `ap-southeast-2` here | Terraform, Helm and the scripts take the Region from the CLI profile; nothing is created elsewhere |
| IAM identity providers cannot be created | EKS Pod Identity for every workload; the cluster is created with `enable_irsa = false` |
| Spot instances are denied | `node_capacity_type = "ON_DEMAND"` (the default) |
| AWS IoT Greengrass is not supported | ICU gateway runs as a pod (`icuGateway.mode=container`, the default). Keep `enable_greengrass_core = false` |
| AWS End User Messaging (SMS) is a Paid-plan service; the SNS SMS calls are denied with it | `alertService.smsBackend: log` (the default). Pages go by e-mail, console and watch |
| SES starts in the sandbox | Verify the sender and every care-team address, or request production access |
| Claude on Bedrock needs Anthropic's one-time use-case form, which this project has not submitted | Agents fall back to their offline path. Until access is granted, deploy with `LLM_PROVIDER=offline` (or `anthropic` with an API key) to avoid a failed call per run. See below |
| No HIPAA BAA in this experience | Synthetic data only (`docs/security/compliance-matrix.md`) |

**Claude on Bedrock, once access is granted.** In `ap-southeast-2` the Claude models are offered through
inference profiles (`au.anthropic.claude-opus-5` stays in Australia; `global.anthropic.claude-opus-5` may route
anywhere). On 2026-10-10 neither the Bedrock Mantle endpoint nor `bedrock-runtime` would serve a Claude model to
this project, so which client and model ID work here is still untested. Check with one request before a demo and
set `agentService.llmModel` accordingly.

**Cost.** `cloud-up` prints the plan and the remaining credits before it creates anything. The estimate of about
US$10/day while the environment is up was made with `us-east-1` prices and has not been re-measured in
`ap-southeast-2`; check AWS Settings > Billing after the first session. Run `make cloud-down` after every
session.

## Evaluation and verification

| What | Command | Needs |
|---|---|---|
| Unit and contract tests | `make test`, and `cd edge/android && ./gradlew :shared:test` | nothing |
| Alarm burden and sensitivity (offline replay) | `uv run rpm-eval replay --hours 24 --out eval/results/replay.json` | dataset |
| Chaos | `uv run rpm-eval chaos --out eval/results/chaos.json` | local stack plus simulator |
| Load | `uv run rpm-eval load --devices 1000` | local stack |
| Audit chain | query `audit_exports`; each `prev_sha256` equals the previous batch's `sha256` | database |
