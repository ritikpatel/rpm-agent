# Runbook

## Local stack

| Task | Command |
|---|---|
| First-time set-up | `make setup && make models` |
| Start | `make up` (offline agents, local inbox), `make up LLM=bedrock` (Claude on Amazon Bedrock), add `MAIL=ses` for real e-mail |
| Load the cohort | `make seed` (30 of the patients in `Training Data/`; loads their recorded readings as history and restarts each patient where that data ends) |
| Start / stop the simulated devices | `make sim` / `make sim-stop` |
| Automated end-to-end check | `make e2e` |
| Status and logs | `make ps`, `make logs`, `make stats` |
| Stop | `make down` (keeps data) or `make reset` (deletes this stack's data) |

Addresses: console <http://localhost:3000>, alert e-mail <http://localhost:8025>, FHIR <http://localhost:8080>,
Care API <http://localhost:8000> (interactive docs at `/docs` locally).

### Using the Claude agents locally

`make up LLM=bedrock` passes your AWS sign-in to the containers. Sign in first with
`aws login --profile rpm-capstone`. The sign-in lasts 12 hours; after it expires the agents fall back to offline
rules until you sign in again and re-run `make up LLM=bedrock`.

`make up` records how the stack was started (agents, mail, the temporary AWS sign-in) in the git-ignored
`deploy/compose/.run.env`, so `make seed`, `make sim` and the rest keep the same settings. Run `make up …` again to
change them.

### Alert e-mail to real inboxes

By default alert e-mail stays in a local inbox (<http://localhost:8025>). To send it to the care team's own
mailboxes through Amazon SES:

1. List the people on each escalation tier in `deploy/config/care-team.yaml` (copy `care-team.example.yaml`; the
   file is git-ignored).
2. Put the sender in `deploy/compose/.env` as `RPM_EMAIL_SENDER=<address>` (also git-ignored).
3. Verify every address once in Amazon SES. AWS e-mails each person a link; `make mail-status` shows who has
   clicked. An unverified recipient receives nothing, and the alert's Notifications table says so; the others on
   the tier are still paged.
4. Start with `make up MAIL=ses` (combine with `LLM=bedrock` as needed).

With real inboxes the escalation timers run at their real length (5 minutes a tier) and an unacknowledged alert is
repeated once rather than three times, so nobody's mailbox is flooded. Override with
`RPM_ESCALATION_TIMEOUT_SCALE` and `RPM_MAX_REMINDERS`.

The sender cannot be an `@isb.edu` address: ISB's mail policy rejects mail that claims to come from `isb.edu` but
was not sent by ISB's own systems. Mail from a personal address sent through SES may land in Junk the first time;
marking it "not junk" once fixes that. A domain you own, verified in SES with DKIM, avoids the problem entirely.

Each person gets their own message, containing only the patient's initials, the last four characters of the id,
the severity and a link to the console. On the local stack that link points to `localhost`, so only the machine
running the stack can open it; on AWS it is the public console address.

### Settings worth knowing

Set these in the shell before `make up`.

| Variable | Default | Meaning |
|---|---|---|
| `RPM_COHORT_ICU`, `RPM_COHORT_HOME` | 6, 24 | Training-data patients shown in ICU and monitored at home (`RPM_COHORT_HOME=0` = everyone else in the data, 263 in all) |
| `RPM_ESCALATION_TIMEOUT_SCALE` | 0.2 (1 with `MAIL=ses`) | Shortens escalation timers for demonstrations (0.2 turns 5 minutes into 1) |
| `RPM_MODEL_FAST`, `RPM_MODEL_REASONING` | Claude Haiku 4.5, Claude Sonnet 4.6 | Bedrock model ids for the agents |
| `RPM_EMBEDDINGS` | hash | `titan` uses Amazon Titan Embeddings for guideline search; re-run `make seed` after changing it |
| `RPM_SIM_AUTO_SCENARIOS` | 0 | Random deterioration scenarios per hour |
| `RPM_AGENT_DAILY_LIMIT`, `RPM_AGENT_MONITOR_INTERVAL_S` | 0 (none), 600 | Limits on automatic agent runs; see the AWS cost section |

### Replacing a model

1. Put the new plug-in in `models/plugins/<name>/<version>/` (see [models/CONTRACT.md](../models/CONTRACT.md)).
2. `make model-check`
3. `docker compose -f deploy/compose/docker-compose.yml restart risk-service`
4. `curl localhost:8001/healthz` lists the models served and any plug-in it rejected, with the reason.

### Common problems

| Symptom | Likely cause | Fix |
|---|---|---|
| A service keeps restarting | Dependency not ready or bad configuration | `make logs`; `make ps` |
| Patients listed but none streaming | Simulator not started | `make sim` |
| Alerts open but no summary | Agent service down, or the agent queue is not being read | `docker compose -f deploy/compose/docker-compose.yml logs agent-service` |
| Summaries say "offline-rules" | Started without `LLM=bedrock`, or the AWS sign-in expired | Sign in and `make up LLM=bedrock` |
| Discharge or lab result has no effect | FHIR subscriptions missing | `make seed` |
| FHIR server slow to start | It needs about a minute on first start | Wait; `make seed` waits for it |
| Port already in use | An older stack is running | `docker ps`, stop the other containers |

### Where state lives

- PostgreSQL volume `rpm_db`: vitals, scores, alerts, audit log, guideline index, and the FHIR server's database.
- The stream processor keeps working state in memory and reloads what matters from PostgreSQL after a restart.
- Messages not yet processed stay on the queues; a message that fails repeatedly moves to its dead-letter queue.

### Evaluation

```bash
uv run rpm-eval replay --hours 24 --out docs/eval/replay.json   # alert quality on all 263 patients, no stack needed
uv run rpm-eval latency --out docs/eval/latency.json            # from the running stack
uv run rpm-eval agents --out docs/eval/agents.json              # audit of recorded agent runs
uv run rpm-eval guardrails --out docs/eval/guardrails.json      # red-team of the policy gate
uv run rpm-eval models --out docs/eval/models.json              # what each model plug-in reports
```

## AWS

Everything runs in the project's selected Region, `ap-southeast-2`, from one CloudFormation stack named `rpm`
(`infra/cloudformation/rpm.yaml`). The containers run on k3s (single-node Kubernetes) on one EC2 instance.

| Task | Command |
|---|---|
| Sign in (lasts 12 hours) | `aws login --profile rpm-capstone` |
| Create or update everything | `make cloud-up` (stack, images, containers, cohort) |
| Roll out a code change | `make cloud-deploy` |
| Change a setting only | `SKIP_BUILD=1 make cloud-deploy` |
| Reload the cohort | `make cloud-seed` |
| Console address, containers, memory | `make cloud-status` |
| A service's log | `make cloud-logs S=care-api` (also `stream-processor`, `risk-service`, `agent-service`, `simulator`, `fhir`) |
| Create a console account | `make cloud-user U=first.last E=<e-mail> G="physician presenter admin"` |
| Automated end-to-end check | `make cloud-e2e` |
| Query the hospital record | `make cloud-fhir Q='Flag?patient=RPM-10001725'` |
| Pause / resume the simulated devices | `make cloud-sim-pause` / `make cloud-sim-resume` |
| Send a reading from your Mac | `make sim-cloud ARGS="send --target SYN-000160 --hr 150 --spo2 84"` |
| Stop / start the instance | `make cloud-stop` / `make cloud-start` |
| Delete everything | `make cloud-down CONFIRM=rpm` |

### What `make cloud-up` creates

| Resource | Purpose |
|---|---|
| EC2 `m7i-flex.large` (2 vCPU, 8 GB), 30 GB encrypted disk, Elastic IP, its own small VPC | Runs k3s and every container. No SSH port: administration is through Systems Manager |
| IoT Core rule `rpm_patient_events`, policies `rpm-device` and `rpm-simulator` | Devices publish over mutual TLS; the rule stamps each message and puts it on the queue |
| SQS `rpm-patient-events`, `rpm-agent-tasks`, and a dead-letter queue for each | The unified stream, and work for the agents |
| S3 `rpm-artifacts-<account>` | Release bundles, model plug-ins, protocols and the training data the cohort is built from |
| S3 `rpm-audit-<account>` (versioned) | Hourly hash-chained audit exports; the platform's role can write but not delete |
| ECR `rpm/platform`, `rpm/console` | The two images |
| Cognito user pool `rpm-care-team`, groups `physician`, `nurse`, `admin`, `presenter` | Sign-in and roles |
| CloudFront distribution | HTTPS for the console |
| Budget `rpm-monthly` (US$40) | E-mails the sender address at 50% and 90% |
| Parameter Store `/rpm/*` (encrypted) | Service token, database password, CloudFront header, care-team contacts, the simulated fleet's key |

The instance type is the largest the Free plan allows with enough memory; `t4g.medium` and other types that are
not free-tier eligible are refused at launch.

### How a deployment works

1. `make cloud-deploy` builds both images for the instance's architecture (x86) and pushes them to ECR, tagged
   with the git commit and the time.
2. It uploads the training data, model plug-ins and protocols to the artifacts bucket, and the care team's
   contacts to Parameter Store.
3. It bundles `deploy/k8s` with the settings read from the stack's outputs, uploads the bundle, and asks the
   instance (Systems Manager Run Command) to run `infra/scripts/node-apply.sh`.
4. On the instance that script fetches the secrets with the instance's own role, refreshes the ECR pull secret,
   runs `kubectl apply -k` and waits until every workload is ready.

Containers call AWS (SQS, Bedrock, SES, S3, IoT) with the instance role; there are no access keys anywhere.

### Sign-in

There is no self sign-up. `make cloud-user` creates an account and Cognito e-mails a temporary password; the
person chooses their own at first sign-in. Groups decide what they can do: `physician` and `nurse` read and act,
`admin` reads and sees the audit trail, `presenter` has the demo controls. A demo lead is in
`physician presenter admin`.

### Cost

| While | About |
|---|---|
| The instance is running | US$2.90 a day for compute, plus US$0.25 a day for disk and address |
| The instance is stopped (`make cloud-stop`) | US$0.25 a day |
| Each alert the agents work on | US$0.13 in Bedrock calls (estimate) |

With the cohort of 30, the eight home patients who were unwell when their recorded data ended alert as soon as
monitoring starts, each with e-mail to the paged tier; after that, new alerts come from scenarios you start. The first ten minutes after loading the cohort are the busiest (several patients are already unwell in the
data): about US$2 of Bedrock calls in the first deployment. **Stop the instance, or at least pause the simulated
devices, when nobody is watching.** IoT Core, SQS, S3, Cognito, CloudFront and SES stay within their free
allowances at this scale; the SES sandbox also limits the project to 200 e-mails a day.

Two settings bound what the agents can spend on AWS. Neither affects alerts or e-mail, which never wait for the
agents. Change them with, for example, `RPM_AGENT_DAILY_LIMIT=200 SKIP_BUILD=1 make cloud-deploy`.

| Setting | On AWS | Meaning |
|---|---|---|
| `RPM_AGENT_DAILY_LIMIT` | 80 | Automatic agent runs (alert, check-in, discharge) in any 24 hours; 0 = no limit. A clinician's "ask the agents" and a patient's reply always run |
| `RPM_AGENT_MONITOR_INTERVAL_S` | 3600 | Per patient, at most one agent check-in that no alert started (a rising score) in this many seconds |

When the daily limit is reached, care-api logs "daily agent-run limit reached" and new alerts open and page as
usual but show no agent summary until older runs fall out of the 24-hour window.

### Differences from the local stack

- Sign-in is Cognito, not the role buttons.
- The FHIR server is not reachable from the internet (it has no sign-in of its own): use `make cloud-fhir`.
- Guideline search uses Amazon Titan embeddings; the agents always use Bedrock.
- Escalation timers run at their real length (5 minutes a tier), with one reminder.
- Interactive API docs are off.

### Common problems on AWS

| Symptom | Likely cause | Fix |
|---|---|---|
| Any `make cloud-*` says the sign-in has expired | 12-hour sign-in | `aws login --profile rpm-capstone` |
| Console shows "403" or does not load after `cloud-start` | Containers still starting | Wait three minutes; `make cloud-status` |
| Sign-in page says the redirect is not allowed | Opened by IP or another address | Use the address `make cloud-status` prints |
| Patients listed but none streaming | Simulated devices paused or not restarted | `make cloud-sim-resume`; `make cloud-logs S=simulator` |
| No agent summary | Bedrock refused the call, or the daily agent-run limit was reached | `make cloud-logs S=agent-service`; `make cloud-logs S=care-api` |
| A pod is `ImagePullBackOff` | ECR pull secret expired after a long stop | `SKIP_BUILD=1 make cloud-deploy` |
| `cloud-up` fails with "not eligible for Free Tier" | Instance type not allowed on the Free plan | Keep the default `m7i-flex.large` |
