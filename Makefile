# Remote patient monitoring platform: local development and the AWS deployment.
SHELL := /bin/bash
COMPOSE := docker compose -f deploy/compose/docker-compose.yml
AWS_PROFILE ?= rpm-capstone
DATA ?= Training Data
LLM ?= offline
MAIL ?= local
SIM := uv run rpm-sim

.DEFAULT_GOAL := help
.PHONY: help setup test lint models model-check build up seed sim sim-stop scenario clear scenarios cohort \
        discharge send stats logs ps down reset e2e mail-status

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
build:           ## build the images
	$(COMPOSE) build care-api console

# LLM=bedrock uses the Claude agents on Amazon Bedrock; the default, offline, uses deterministic rules and templates.
# MAIL=ses sends alert e-mail to the care team in deploy/config/care-team.yaml through Amazon SES, from the address in
# RPM_EMAIL_SENDER (set it in deploy/compose/.env); the default, local, keeps e-mail in the inbox at localhost:8025.
# Both use your AWS sign-in: run `aws login --profile $(AWS_PROFILE)` first.
up:              ## start the local stack: make up [LLM=bedrock] [MAIL=ses]
	@if [ "$(LLM)" = "bedrock" ] || [ "$(MAIL)" = "ses" ]; then \
	  eval "$$(aws configure export-credentials --profile $(AWS_PROFILE) --format env)" || exit 1; \
	fi; \
	if [ "$(LLM)" = "bedrock" ]; then export RPM_LLM_PROVIDER=bedrock; fi; \
	if [ "$(MAIL)" = "ses" ]; then export RPM_EMAIL_BACKEND=ses; fi; \
	$(COMPOSE) up -d --build postgres mosquitto sqs mailpit fhir iot-rule-emulator care-api stream-processor \
	  risk-service agent-service console
	@echo "console http://localhost:3000   alert e-mail http://localhost:8025   FHIR http://localhost:8080"

seed:            ## load the cohort: registry, FHIR record, guideline index, FHIR subscriptions
	$(COMPOSE) --profile seed run --rm seed

sim:             ## start the simulated devices (ICU monitors for admitted patients, wearables at home)
	$(COMPOSE) --profile sim up -d simulator

sim-stop:        ## stop the simulated devices
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

logs:            ## follow the platform logs
	$(COMPOSE) logs -f --tail 50 care-api stream-processor risk-service agent-service

ps:              ## container status
	@$(COMPOSE) --profile sim ps --format 'table {{.Service}}\t{{.Status}}'

down:            ## stop everything (keeps the data)
	$(COMPOSE) --profile sim --profile seed down --remove-orphans

reset:           ## stop everything and delete this stack's local data
	$(COMPOSE) --profile sim --profile seed down --remove-orphans --volumes
