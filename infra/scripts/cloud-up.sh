#!/usr/bin/env bash
# Bring the AWS demo environment up (idempotent; safe to re-run to redeploy).
#   AWS_PROFILE=rpm-capstone infra/scripts/cloud-up.sh        (Region: AWS_REGION, else the profile's, else ap-southeast-2)
# Env: AUTO_APPROVE=1 (skip terraform prompt), SKIP_INFRA=1, SKIP_BUILD=1, IMAGE_TAG=..., TFVARS=path/to/file.tfvars,
#      OBSERVABILITY=1 (kube-prometheus-stack + latency dashboard), ICU_GATEWAY_MODE=container|off
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || true)}"; REGION="${REGION:-ap-southeast-2}"
ENVIRONMENT="${RPM_ENV_NAME:-demo}"
TAG="${IMAGE_TAG:-$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M)}"
SECRETS_DIR="$ROOT/.secrets"          # git-ignored; device certs for the watch app and simulator
TF=(terraform -chdir="$ROOT/infra/terraform/aws")
APPROVE=(); [[ "${AUTO_APPROVE:-0}" == "1" ]] && APPROVE=(-auto-approve)

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
need() { command -v "$1" >/dev/null || { echo "missing required tool: $1"; exit 1; }; }
for t in aws terraform kubectl helm docker; do need "$t"; done
docker info >/dev/null 2>&1 || { echo "Docker is not running"; exit 1; }

export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"   # every aws / terraform call below uses this Region
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
STATE_BUCKET="rpm-tfstate-${ACCOUNT}"
# New AWS experience: show the plan and remaining credits before creating billable resources (no-op elsewhere)
PLAN="$(aws freetier get-account-plan-state --query '[accountPlanType,accountPlanRemainingCredits.amount]' \
  --output text 2>/dev/null || true)"
[[ -n "$PLAN" ]] && echo "AWS plan / remaining credits (USD): ${PLAN}"
step "account ${ACCOUNT} | region ${REGION} | env ${ENVIRONMENT} | image tag ${TAG}"

if [[ "${SKIP_INFRA:-0}" != "1" ]]; then
  step "1/7 terraform state bucket"
  if aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
    echo "exists: s3://${STATE_BUCKET}"
  else
    terraform -chdir="$ROOT/infra/terraform/bootstrap" init -input=false >/dev/null
    terraform -chdir="$ROOT/infra/terraform/bootstrap" apply -input=false -auto-approve -var "region=${REGION}"
  fi

  step "2/7 platform infrastructure (VPC, EKS, RDS, ElastiCache, IoT Core, SQS, S3, KMS) ~20 min first time"
  "${TF[@]}" init -input=false -reconfigure \
    -backend-config="bucket=${STATE_BUCKET}" -backend-config="key=rpm/${ENVIRONMENT}.tfstate" \
    -backend-config="region=${REGION}" -backend-config="use_lockfile=true" -backend-config="encrypt=true" >/dev/null
  "${TF[@]}" apply -input=false "${APPROVE[@]}" -var "region=${REGION}" -var "environment=${ENVIRONMENT}" \
    ${TFVARS:+-var-file="$TFVARS"}
fi

out() { "${TF[@]}" output -raw "$1"; }
CLUSTER="$(out cluster_name)"; REGISTRY="$(out ecr_registry)"; DATA_BUCKET="$(out data_bucket)"
NS="$(out namespace)"; IOT_ENDPOINT="$(out iot_endpoint)"

step "3/7 kubeconfig"
aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION" --alias "rpm-${ENVIRONMENT}" >/dev/null
kubectl get nodes -o wide

step "4/7 dataset, model packages, knowledge corpus -> s3://${DATA_BUCKET} (SSE-KMS, TLS-only bucket)"
[[ -d "$ROOT/models/registry" ]] || (cd "$ROOT" && uv run --group train python models/train_baselines.py)
aws s3 sync "$ROOT/synthetic_mimic_150patients" "s3://${DATA_BUCKET}/mimic/" --only-show-errors
aws s3 sync "$ROOT/models/registry" "s3://${DATA_BUCKET}/models/" --only-show-errors
aws s3 sync "$ROOT/knowledge" "s3://${DATA_BUCKET}/knowledge/" --only-show-errors

if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  step "5/7 build + push images (linux/arm64 for Graviton nodes) -> ${REGISTRY}"
  aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
  for pair in stream-engine:stream-engine rpm-api:api rpm-simulator:simulator fhir-loader:fhir-loader \
              rpm-common:db-migrate model-serving:model-serving alert-service:alert-service \
              agent-service:agent-service rpm-rag:rag rpm-icu-gateway:icu-gateway; do
    pkg="${pair%%:*}"; repo="${pair##*:}"
    docker build -q --platform linux/arm64 -f "$ROOT/deploy/docker/python-service.Dockerfile" \
      --build-arg PACKAGE="$pkg" -t "${REGISTRY}/rpm/${repo}:${TAG}" "$ROOT" >/dev/null
    docker push -q "${REGISTRY}/rpm/${repo}:${TAG}" >/dev/null
    echo "pushed rpm/${repo}:${TAG}"
  done
  docker build -q --platform linux/arm64 -f "$ROOT/deploy/docker/console.Dockerfile" \
    -t "${REGISTRY}/rpm/console:${TAG}" "$ROOT" >/dev/null
  docker push -q "${REGISTRY}/rpm/console:${TAG}" >/dev/null && echo "pushed rpm/console:${TAG}"
fi

step "6/7 namespace, database secret, device certificates"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl label namespace "$NS" --overwrite pod-security.kubernetes.io/enforce=baseline \
  pod-security.kubernetes.io/warn=restricted pod-security.kubernetes.io/audit=restricted >/dev/null
kubectl -n "$NS" create secret generic rpm-db \
  --from-literal=DATABASE_URL="$(out database_url)" \
  --from-literal=JDBC_URL="jdbc:postgresql://$(out db_host):5432/hapi?sslmode=require" \
  --from-literal=DB_USER="$(out db_username)" --from-literal=DB_PASSWORD="$(out db_password)" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
mkdir -p "$SECRETS_DIR" && chmod 700 "$SECRETS_DIR"
curl -fsS https://www.amazontrust.com/repository/AmazonRootCA1.pem -o "$SECRETS_DIR/AmazonRootCA1.pem"
( umask 077
  out sim_cert_pem > "$SECRETS_DIR/sim-fleet.cert.pem"; out sim_private_key > "$SECRETS_DIR/sim-fleet.key.pem"
  DEMO="$(out demo_device_id)"
  out demo_watch_cert_pem > "$SECRETS_DIR/${DEMO}.cert.pem"; out demo_watch_private_key > "$SECRETS_DIR/${DEMO}.key.pem"
  out icu_gateway_cert_pem > "$SECRETS_DIR/$(out icu_gateway_id).cert.pem"
  out icu_gateway_private_key > "$SECRETS_DIR/$(out icu_gateway_id).key.pem"
  echo "$IOT_ENDPOINT" > "$SECRETS_DIR/iot-endpoint.txt" )
kubectl -n "$NS" create secret generic rpm-sim-mqtt \
  --from-file=ca.pem="$SECRETS_DIR/AmazonRootCA1.pem" --from-file=cert.pem="$SECRETS_DIR/sim-fleet.cert.pem" \
  --from-file=key.pem="$SECRETS_DIR/sim-fleet.key.pem" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
GW="$(out icu_gateway_id)"
kubectl -n "$NS" create secret generic rpm-icu-gw-mqtt \
  --from-file=ca.pem="$SECRETS_DIR/AmazonRootCA1.pem" --from-file=cert.pem="$SECRETS_DIR/${GW}.cert.pem" \
  --from-file=key.pem="$SECRETS_DIR/${GW}.key.pem" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
echo "device certificates written to .secrets/ (git-ignored); provision the watch with edge/android/provision.sh aws"
# ICU gateway: a pod on EKS unless Terraform created a Greengrass core (enable_greengrass_core = true)
GG_IP="$(out greengrass_core_private_ip)"
if [[ -n "$GG_IP" ]]; then
  ICU_ARGS=(--set icuGateway.mode=greengrass --set "icuGateway.hl7Endpoint=${GG_IP}:2575")
else
  ICU_ARGS=(--set "icuGateway.mode=${ICU_GATEWAY_MODE:-container}")
fi

OBS_ARGS=()
if [[ "${OBSERVABILITY:-0}" == "1" ]]; then   # opt-in: ~1 GiB of cluster memory
  step "observability: kube-prometheus-stack (Prometheus + Grafana, latency dashboard via ConfigMap)"
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo update prometheus-community >/dev/null
  helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack -n monitoring \
    --create-namespace --wait --timeout 10m --set alertmanager.enabled=false \
    --set prometheus.prometheusSpec.retention=2d \
    --set prometheus.prometheusSpec.podMonitorSelectorNilUsesHelmValues=false \
    --set grafana.sidecar.dashboards.searchNamespace=ALL >/dev/null
  OBS_ARGS=(--set observability.enabled=true)
fi

step "7/7 helm release (migrations -> services -> FHIR load)"
helm upgrade --install rpm "$ROOT/deploy/helm/rpm" -n "$NS" --wait --timeout 25m \
  --set image.registry="$REGISTRY" --set image.tag="$TAG" --set aws.region="$REGION" \
  --set config.redisUrl="$(out redis_url)" --set config.mimicDir="s3://${DATA_BUCKET}/mimic" \
  --set config.iotEndpoint="$IOT_ENDPOINT" --set simulator.mqttHost="$IOT_ENDPOINT" \
  --set modelServing.registry="s3://${DATA_BUCKET}/models" \
  --set ragIndex.protocols="s3://${DATA_BUCKET}/knowledge/protocols" \
  --set alertService.emailSender="$(out alert_sender_email)" \
  --set alertService.escalationTimeoutScale="${ESCALATION_TIMEOUT_SCALE:-0.2}" \
  --set agentService.llmProvider="${LLM_PROVIDER:-bedrock}" \
  --set auth.issuer="$(out cognito_issuer)" --set auth.clientId="$(out cognito_client_id)" \
  --set auth.domain="$(out cognito_domain)" --set auditExport.bucket="$(out audit_bucket)" \
  --set telemetryArchiver.bucket="$(out lake_bucket)" \
  "${ICU_ARGS[@]}" "${OBS_ARGS[@]}" ${HELM_EXTRA_ARGS:-}
if [[ -n "$GG_IP" ]]; then
  step "ICU gateway component -> Greengrass core ${GW} (${GG_IP})"
  "$ROOT/edge/icu-gateway/greengrass/deploy.sh"
fi
kubectl -n "$NS" get pods

cat <<MSG

AWS demo environment is up.
  console:     make cloud-board   ->  http://localhost:3000   (ops live board: kubectl -n ${NS} port-forward svc/api 8000:80)
  scenario:    make cloud-scenario P=p-90000001 S=sepsis
  tear down:   make cloud-down   (stops all hourly charges except the few-cent state bucket)
MSG
