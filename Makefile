SHELL := /bin/bash
COMPOSE := docker compose -f deploy/compose/docker-compose.yml
P ?= p-90000001
S ?= sepsis

.PHONY: help setup test lint up seed sim sim-auto down reset logs ps scenario clear scenarios cohort board stats

help:            ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

setup:           ## install the Python workspace (uv)
	uv sync

test:            ## run unit + contract tests
	uv run pytest

lint:            ## ruff lint
	uv run ruff check .

up:              ## start infra + platform services (local stack)
	$(COMPOSE) up -d --build postgres redis mosquitto kinesis sqs fhir db-migrate iot-bridge stream-engine api
	@echo "live board: http://localhost:8000   FHIR: http://localhost:8080/fhir   metrics: :9101/metrics"

seed:            ## load the cohort into FHIR + seed the device registry (RPM_DEMO_PATIENT=auto to reserve one for the watch)
	$(COMPOSE) --profile seed run --rm --build fhir-loader

sim:             ## start the 150-patient device fleet simulator
	$(COMPOSE) --profile sim up -d --build simulator

sim-auto:        ## simulator with ~6 random deterioration scenarios per hour
	RPM_SIM_AUTO_SCENARIOS=6 $(COMPOSE) --profile sim up -d --build simulator

scenario:        ## inject a scenario: make scenario P=p-90000001 S=sepsis [RAMP=90]
	$(COMPOSE) --profile sim exec simulator rpm-sim scenario --target $(P) --type $(S) $(if $(RAMP),--ramp-s $(RAMP),)

clear:           ## clear a patient's scenario: make clear P=p-90000001
	$(COMPOSE) --profile sim exec simulator rpm-sim clear --target $(P)

scenarios:       ## list available scenarios
	uv run rpm-sim scenarios

cohort:          ## print the deterministic demo cohort
	uv run rpm-sim cohort

stats:           ## pipeline stats (latency, bands, events)
	@curl -s localhost:8000/api/stats | python3 -m json.tool

logs:            ## follow platform logs
	$(COMPOSE) --profile sim logs -f --tail=50 iot-bridge stream-engine api simulator

ps:
	$(COMPOSE) --profile sim --profile seed ps

down:            ## stop everything (keeps the database volume)
	$(COMPOSE) --profile sim --profile seed down

reset:           ## stop everything and delete local data
	$(COMPOSE) --profile sim --profile seed down -v

# ------------------------------------------------------------------ AWS demo environment
cloud-up:        ## create/update the AWS environment and deploy (AWS_PROFILE, AWS_REGION; AUTO_APPROVE=1)
	infra/scripts/cloud-up.sh

cloud-deploy:    ## redeploy images + helm only (infra unchanged)
	SKIP_INFRA=1 infra/scripts/cloud-up.sh

cloud-down:      ## destroy the AWS environment (stops hourly charges)
	infra/scripts/cloud-down.sh

cloud-status:    ## pods and recent events in the cluster
	kubectl -n rpm get pods,svc,jobs && kubectl -n rpm get events --sort-by=.lastTimestamp | tail -15

cloud-board:     ## port-forward the live board to http://localhost:8000
	kubectl -n rpm port-forward svc/api 8000:80

cloud-scenario:  ## inject a scenario on AWS via IoT Core: make cloud-scenario P=p-90000001 S=sepsis [RAMP=90]
	aws iot-data publish --topic rpm/v1/sim/control --cli-binary-format raw-in-base64-out \
	  --endpoint-url "https://$$(cat .secrets/iot-endpoint.txt)" \
	  --payload '{"action":"scenario","target":"$(P)","scenario":"$(S)"$(if $(RAMP),$(comma)"ramp_s":$(RAMP),)}'

comma := ,
