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
  kubectl --context "rpm-${ENVIRONMENT}" delete namespace rpm --wait=true --timeout=5m 2>/dev/null || true
fi
"${TF[@]}" destroy -input=false "${APPROVE[@]}" -var "region=${REGION}" -var "environment=${ENVIRONMENT}"
rm -rf "$ROOT/.secrets"
echo "AWS demo environment destroyed."
