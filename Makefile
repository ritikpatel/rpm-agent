# Remote patient monitoring platform: local development and the AWS deployment.
SHELL := /bin/bash
COMPOSE := docker compose -f deploy/compose/docker-compose.yml
AWS_PROFILE ?= rpm-capstone
MIMIC ?= synthetic_mimic_150patients
LLM ?= offline
SIM := uv run rpm-sim

.DEFAULT_GOAL := help
.PHONY: help setup test lint models model-check build up seed sim sim-stop scenario clear scenarios cohort \
        discharge send stats logs ps down reset e2e eval audit-export

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
	uv run python models/train_placeholders.py --mimic-dir $(MIMIC)

model-check:     ## check every model plug-in against models/CONTRACT.md
	uv run pytest services/risk-service/tests/test_plugins.py -q

# ------------------------------------------------------------------ local stack
build:           ## build the images
	$(COMPOSE) build care-api console

# LLM=bedrock uses Amazon Bedrock with your AWS sign-in (run `aws login --profile $(AWS_PROFILE)` first);
# the default, offline, uses deterministic rules and templates and needs no credentials.
up:              ## start the local stack: make up [LLM=bedrock]
	@if [ "$(LLM)" = "bedrock" ]; then \
	  eval "$$(aws configure export-credentials --profile $(AWS_PROFILE) --format env)" || exit 1; \
	  export RPM_LLM_PROVIDER=bedrock RPM_EMBEDDINGS=$${RPM_EMBEDDINGS:-hash}; \
	fi; \
	$(COMPOSE) up -d --build postgres mosquitto sqs mailpit fhir iot-rule-emulator care-api stream-processor \
	  risk-service agent-service console
	@echo "console http://localhost:3000   alert e-mail http://localhost:8025   FHIR http://localhost:8080"

seed:            ## load the cohort: registry, FHIR record, guideline index, FHIR subscriptions
	$(COMPOSE) --profile seed run --rm seed

sim:             ## start the simulated devices (ICU monitors for admitted patients, wearables at home)
	$(COMPOSE) --profile sim up -d simulator

sim-stop:        ## stop the simulated devices
	$(COMPOSE) --profile sim stop simulator

scenario:        ## make a patient deteriorate: make scenario P=p-90000001 S=sepsis [RAMP=60]
	$(SIM) scenario --target $(P) --type $(S) $(if $(RAMP),--ramp-s $(RAMP),)

clear:           ## end a patient's scenario: make clear P=p-90000001
	$(SIM) clear --target $(P)

scenarios:       ## list the scenarios
	@$(SIM) scenarios

cohort:          ## list the monitored patients
	@$(SIM) cohort --mimic-dir $(MIMIC)

send:            ## send one reading now: make send P=p-90000001 ARGS="--hr 134 --spo2 86"
	$(SIM) send --target $(P) $(ARGS)

stats:           ## pipeline numbers (patients streaming, latency, events)
	@TOKEN=$$(curl -s -X POST localhost:8000/api/auth/dev/token -H 'content-type: application/json' \
	  -d '{"username":"make","role":"admin"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])'); \
	curl -s localhost:8000/api/stats -H "authorization: Bearer $$TOKEN" | python3 -m json.tool

e2e:             ## end-to-end check of the running local stack
	uv run python tools/e2e/local_e2e.py

logs:            ## follow the platform logs
	$(COMPOSE) logs -f --tail 50 care-api stream-processor risk-service agent-service

ps:              ## container status
	@$(COMPOSE) --profile sim ps --format 'table {{.Service}}\t{{.Status}}'

down:            ## stop everything (keeps the data)
	$(COMPOSE) --profile sim --profile seed down --remove-orphans

reset:           ## stop everything and delete this stack's local data
	$(COMPOSE) --profile sim --profile seed down --remove-orphans --volumes
