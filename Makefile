SHELL := /bin/bash
COMPOSE := docker compose -f deploy/compose/docker-compose.yml
P ?= p-90000001
S ?= sepsis

.PHONY: help setup test lint models up seed sim sim-auto down reset logs ps scenario clear scenarios cohort board stats

help:            ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

setup:           ## install the Python workspace (uv)
	uv sync

test:            ## run unit + contract tests
	uv run pytest

lint:            ## ruff lint
	uv run ruff check .

models:          ## train/export the baseline risk models into models/registry (data team replaces these)
	uv run --group train python models/train_baselines.py

up:              ## start infra + platform services (local stack); LLM: RPM_LLM_PROVIDER=offline|anthropic|bedrock
	@test -d models/registry || $(MAKE) models
	$(COMPOSE) up -d --build postgres redis mosquitto kinesis sqs fhir mailpit db-migrate iot-bridge stream-engine \
	  model-serving alert-service agent-service api console
	@echo "tip: agents run offline (rules + templates) unless RPM_LLM_PROVIDER=anthropic (+ANTHROPIC_API_KEY) or bedrock"
	@echo "console: http://localhost:3000   live board: http://localhost:8000   email: http://localhost:8025   FHIR: http://localhost:8080/fhir"

seed:            ## FHIR + device registry + RAG index (RPM_DEMO_PATIENT=auto reserves one patient for the watch)
	$(COMPOSE) --profile seed run --rm --build fhir-loader
	$(COMPOSE) --profile seed run --rm --build rag-index

obs:             ## Prometheus + Grafana latency dashboards (http://localhost:3001)
	$(COMPOSE) --profile obs up -d prometheus grafana

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

audit-export:    ## export settled audit_log rows as FHIR AuditEvent NDJSON (hash-chained) to data/audit/
	uv run rpm-audit-export --dest data/audit

stats:           ## pipeline stats (latency, bands, events)
	@TOKEN=$$(curl -s -XPOST localhost:8000/api/auth/dev/token -H content-type:application/json \
	  -d '{"username":"ops.cli","role":"admin"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])') && \
	  curl -s localhost:8000/api/stats -H "authorization: Bearer $$TOKEN" | python3 -m json.tool

logs:            ## follow platform logs
	$(COMPOSE) --profile sim logs -f --tail=50 stream-engine alert-service agent-service api simulator

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

cloud-board:     ## port-forward the clinician console to http://localhost:3000
	kubectl -n rpm port-forward svc/console 3000:80

cloud-user:      ## invite a console user (Cognito emails a temporary password): make cloud-user U=nurse.jane E=jane@example.org R=nurse
	@test -n "$(U)" -a -n "$(E)" -a -n "$(R)" || { echo "usage: make cloud-user U=<username> E=<email> R=nurse|physician|admin|presenter"; exit 2; }
	POOL=$$(terraform -chdir=infra/terraform/aws output -raw cognito_user_pool_id) && \
	aws cognito-idp admin-create-user --user-pool-id $$POOL --username $(U) --desired-delivery-mediums EMAIL \
	  --user-attributes Name=email,Value=$(E) Name=email_verified,Value=true >/dev/null && \
	aws cognito-idp admin-add-user-to-group --user-pool-id $$POOL --username $(U) --group-name $(R) && \
	echo "invited $(U) as $(R); Cognito has emailed $(E) a temporary password"

cloud-scenario:  ## inject a scenario on AWS via IoT Core: make cloud-scenario P=p-90000001 S=sepsis [RAMP=90]
	aws iot-data publish --topic rpm/v1/sim/control --cli-binary-format raw-in-base64-out \
	  --endpoint-url "https://$$(cat .secrets/iot-endpoint.txt)" \
	  --payload '{"action":"scenario","target":"$(P)","scenario":"$(S)"$(if $(RAMP),$(comma)"ramp_s":$(RAMP),)}'

comma := ,
