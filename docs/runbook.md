# Runbook

## Local stack

| Task | Command |
|---|---|
| First-time set-up | `make setup && make models` |
| Start | `make up` (offline agents, local inbox), `make up LLM=bedrock` (Claude on Amazon Bedrock), add `MAIL=ses` for real e-mail |
| Load the cohort | `make seed` (30 of the patients in `Training Data/`) |
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
| `RPM_SIM_SPEED` | 1 | Play the patients' post-discharge data faster than real time (for example 12) |
| `RPM_ESCALATION_TIMEOUT_SCALE` | 0.2 (1 with `MAIL=ses`) | Shortens escalation timers for demonstrations (0.2 turns 5 minutes into 1) |
| `RPM_MODEL_FAST`, `RPM_MODEL_REASONING` | Claude Haiku 4.5, Claude Sonnet 4.6 | Bedrock model ids for the agents |
| `RPM_EMBEDDINGS` | hash | `titan` uses Amazon Titan Embeddings for guideline search; re-run `make seed` after changing it |
| `RPM_SIM_AUTO_SCENARIOS` | 0 | Random deterioration scenarios per hour |

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

The AWS deployment (one CloudFormation stack, k3s on one EC2 instance) is added to this runbook when it is created.
