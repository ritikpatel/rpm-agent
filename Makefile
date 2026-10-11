# Remote patient monitoring platform: local development and the AWS deployment.
SHELL := /bin/bash
# .env holds your own settings (for example RPM_EMAIL_SENDER); .run.env is written by `make up` and records how the
# stack was started (agents, mail, AWS sign-in), so every later command (seed, sim, logs) uses the same settings.
ENV_FILES := deploy/compose/.env deploy/compose/.run.env
COMPOSE := docker compose --env-file deploy/compose/.env --env-file deploy/compose/.run.env \
	-f deploy/compose/docker-compose.yml
AWS_PROFILE ?= rpm-capstone
DATA ?= Training Data
LLM ?= offline
MAIL ?= local
SIM := uv run rpm-sim

.DEFAULT_GOAL := help
CLOUD := AWS_PROFILE=$(AWS_PROFILE) DATA="$(DATA)" infra/scripts/cloud.sh

.PHONY: help setup test lint models model-check build up seed sim sim-stop scenario clear scenarios cohort \
        discharge send stats logs ps down reset e2e mail-status cloud-up cloud-deploy cloud-seed cloud-status \
        cloud-logs cloud-fhir cloud-stop cloud-start cloud-down cloud-user cloud-device cloud-e2e sim-cloud \
        cloud-sim-pause cloud-sim-resume

help:            ## list targets
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

# ------------------------------------------------------------------ development
setup:           ## install the Python workspace (uv)
	uv sync

test:            ## unit and contract tests
	uv run pytest

lint:            ## ruff
	uv run ruff check .

models:          ## fit the placeholder models into models/plugins (the data team's models replace them)
	uv run python models/train_placeholders.py --data-dir "$(DATA)"

model-check:     ## check every model plug-in against models/CONTRACT.md
	uv run pytest services/risk-service/tests/test_plugins.py -q

# ------------------------------------------------------------------ local stack
build: $(ENV_FILES) ## build the images
	$(COMPOSE) build care-api console

# LLM=bedrock uses the Claude agents on Amazon Bedrock; the default, offline, uses deterministic rules and templates.
# MAIL=ses sends alert e-mail to the care team in deploy/config/care-team.yaml through Amazon SES, from the address in
# RPM_EMAIL_SENDER (set it in deploy/compose/.env); the default, local, keeps e-mail in the inbox at localhost:8025.
# With real inboxes the escalation timers run at their real length (5 minutes a tier) and an alert nobody
# acknowledges is repeated once, not three times, so nobody's mailbox is flooded.
# Both use your AWS sign-in: run `aws login --profile $(AWS_PROFILE)` first.
$(ENV_FILES):
	@touch $@ && chmod 600 $@

up: $(ENV_FILES) ## start the local stack: make up [LLM=bedrock] [MAIL=ses]
	@{ echo "RPM_LLM_PROVIDER=$(if $(filter bedrock,$(LLM)),bedrock,offline)"; \
	   echo "RPM_EMAIL_BACKEND=$(if $(filter ses,$(MAIL)),ses,smtp)"; \
	   $(if $(filter ses,$(MAIL)),echo "RPM_ESCALATION_TIMEOUT_SCALE=$${RPM_ESCALATION_TIMEOUT_SCALE:-1}"; echo "RPM_MAX_REMINDERS=$${RPM_MAX_REMINDERS:-1}";,) \
	   if [ "$(LLM)" = "bedrock" ] || [ "$(MAIL)" = "ses" ]; then \
	     aws configure export-credentials --profile $(AWS_PROFILE) --format env-no-export || exit 1; \
	   fi; } > deploy/compose/.run.env.tmp && mv deploy/compose/.run.env.tmp deploy/compose/.run.env \
	  && chmod 600 deploy/compose/.run.env
	$(COMPOSE) up -d --build postgres mosquitto sqs mailpit fhir iot-rule-emulator care-api stream-processor \
	  risk-service agent-service console
	@echo "console http://localhost:3000   FHIR http://localhost:8080   agents: $(LLM)   alert e-mail: $(if $(filter ses,$(MAIL)),Amazon SES to the care team,local inbox http://localhost:8025)"

seed: $(ENV_FILES) ## load the cohort: registry, recorded readings as history, FHIR record, guideline index, subscriptions
	$(COMPOSE) --profile seed run --rm seed
	@for s in stream-processor simulator; do \
	  [ -z "$$($(COMPOSE) --profile sim ps --status running -q $$s)" ] || $(COMPOSE) --profile sim restart $$s; \
	done   # every patient's timeline has restarted: so do the rules' working state and any running devices

sim: $(ENV_FILES) ## start the simulated devices (ICU monitors for admitted patients, wearables at home)
	$(COMPOSE) --profile sim up -d simulator

sim-stop: $(ENV_FILES) ## stop the simulated devices
	$(COMPOSE) --profile sim stop simulator

scenario:        ## make a patient deteriorate: make scenario P=SYN-000143 S=sepsis [RAMP=60]
	$(SIM) scenario --target $(P) --type $(S) $(if $(RAMP),--ramp-s $(RAMP),)

clear:           ## end a patient's scenario: make clear P=SYN-000143
	$(SIM) clear --target $(P)

scenarios:       ## list the scenarios
	@$(SIM) scenarios

cohort:          ## list the monitored patients
	@$(SIM) cohort --data-dir "$(DATA)"

send:            ## send one reading now: make send P=SYN-000143 ARGS="--hr 134 --spo2 86"
	$(SIM) send --target $(P) $(ARGS)

mail-status:     ## which alert e-mail addresses Amazon SES has verified (unverified ones receive nothing)
	@aws sesv2 list-email-identities --profile $(AWS_PROFILE) --region ap-southeast-2 \
	  --query 'EmailIdentities[].[IdentityName,VerificationStatus]' --output table

stats:           ## pipeline numbers (patients streaming, latency, events)
	@TOKEN=$$(curl -s -X POST localhost:8000/api/auth/dev/token -H 'content-type: application/json' \
	  -d '{"username":"make","role":"admin"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])'); \
	curl -s localhost:8000/api/stats -H "authorization: Bearer $$TOKEN" | python3 -m json.tool

e2e:             ## end-to-end check of the running local stack
	python3 scripts/e2e.py $(ARGS)

logs: $(ENV_FILES) ## follow the platform logs
	$(COMPOSE) logs -f --tail 50 care-api stream-processor risk-service agent-service

ps: $(ENV_FILES) ## container status
	@$(COMPOSE) --profile sim ps --format 'table {{.Service}}\t{{.Status}}'

down: $(ENV_FILES) ## stop everything (keeps the data)
	$(COMPOSE) --profile sim --profile seed down --remove-orphans

reset: $(ENV_FILES) ## stop everything and delete this stack's local data
	$(COMPOSE) --profile sim --profile seed down --remove-orphans --volumes

# ------------------------------------------------------------------ AWS (see docs/runbook.md)
# Sign in first: aws login --profile $(AWS_PROFILE). Everything runs in the project's selected Region, ap-southeast-2.
cloud-up:        ## create or update the AWS resources (CloudFormation stack `rpm`), then deploy and load the cohort
	$(CLOUD) up
	$(CLOUD) deploy
	$(CLOUD) seed

cloud-deploy:    ## build, push and roll out the containers (SKIP_BUILD=1 = settings only)
	SKIP_BUILD=$(SKIP_BUILD) $(CLOUD) deploy

cloud-seed:      ## load the cohort on AWS: registry, FHIR record, guideline index, FHIR subscriptions
	$(CLOUD) seed

cloud-status:    ## console address, instance state, containers and memory
	@$(CLOUD) status

cloud-logs:      ## a service's log: make cloud-logs S=care-api [N=100]
	@$(CLOUD) logs $(S) $(N)

cloud-fhir:      ## query the hospital record on AWS: make cloud-fhir Q='Flag?patient=RPM-10001725'
	@$(CLOUD) fhir '$(Q)'

cloud-stop:      ## stop the instance between demonstrations (keeps all data; stops the compute charge)
	$(CLOUD) stop

cloud-start:     ## start the instance again
	$(CLOUD) start

cloud-user:      ## create a console account: make cloud-user U=ritik.patel E=you@example.org G="physician presenter admin"
	$(CLOUD) user $(U) $(E) "$(or $(G),physician)"

cloud-device:    ## create a real device's certificate in AWS IoT Core: make cloud-device D=gw-demo-01
	$(CLOUD) device $(D)

sim-cloud:       ## the simulator CLI from this machine against AWS: make sim-cloud ARGS="send --target SYN-000160 --event SOS"
	@$(CLOUD) sim $(ARGS)

cloud-sim-pause: ## stop the simulated devices on AWS (no new readings, alerts, e-mail or agent runs)
	$(CLOUD) sim-pause

cloud-sim-resume: ## start the simulated devices on AWS again
	$(CLOUD) sim-resume

cloud-e2e:       ## end-to-end check of the AWS deployment
	$(CLOUD) e2e $(ARGS)

cloud-down:      ## delete the AWS deployment and its data: make cloud-down CONFIRM=rpm
	CONFIRM=$(CONFIRM) $(CLOUD) down
