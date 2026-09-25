#!/usr/bin/env bash
# Destroy the AWS demo environment (keeps only the Terraform state bucket). AUTO_APPROVE=1 to skip the prompt.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REGION="${AWS_REGION:-us-east-1}"
ENVIRONMENT="${RPM_ENV_NAME:-demo}"
TF=(terraform -chdir="$ROOT/infra/terraform/aws")
APPROVE=(); [[ "${AUTO_APPROVE:-0}" == "1" ]] && APPROVE=(-auto-approve)
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"

"${TF[@]}" init -input=false -reconfigure -backend-config="bucket=rpm-tfstate-${ACCOUNT}" \
  -backend-config="key=rpm/${ENVIRONMENT}.tfstate" -backend-config="region=${REGION}" \
  -backend-config="use_lockfile=true" -backend-config="encrypt=true" >/dev/null

# Remove Kubernetes-created AWS resources (load balancers, ENIs) before the VPC goes away.
if kubectl config get-contexts "rpm-${ENVIRONMENT}" >/dev/null 2>&1; then
  helm --kube-context "rpm-${ENVIRONMENT}" uninstall rpm -n rpm --wait 2>/dev/null || true
  helm --kube-context "rpm-${ENVIRONMENT}" uninstall kube-prometheus-stack -n monitoring --wait 2>/dev/null || true
  kubectl --context "rpm-${ENVIRONMENT}" delete namespace rpm --wait=true --timeout=5m 2>/dev/null || true
fi
# Things Terraform does not own: the Greengrass core-device record, component versions, retained gateway config.
GW="$("${TF[@]}" output -raw icu_gateway_id 2>/dev/null || echo icu-gw-01)"
IOT_ENDPOINT="$("${TF[@]}" output -raw iot_endpoint 2>/dev/null || true)"
aws greengrassv2 delete-core-device --region "$REGION" --core-device-thing-name "$GW" 2>/dev/null || true
for arn in $(aws greengrassv2 list-component-versions --region "$REGION" --query 'componentVersions[].arn' \
    --arn "arn:aws:greengrass:${REGION}:${ACCOUNT}:components:com.cloudbox.rpm.IcuGateway" --output text 2>/dev/null); do
  aws greengrassv2 delete-component --region "$REGION" --arn "$arn" || true
done
[[ -n "$IOT_ENDPOINT" ]] && aws iot-data publish --region "$REGION" --endpoint-url "https://${IOT_ENDPOINT}" \
  --topic "rpm/v1/dev/${GW}/config" --retain --qos 1 --cli-binary-format raw-in-base64-out --payload '' 2>/dev/null || true

"${TF[@]}" destroy -input=false "${APPROVE[@]}" -var "region=${REGION}" -var "environment=${ENVIRONMENT}"
rm -rf "$ROOT/.secrets"
echo "AWS demo environment destroyed."
