#!/usr/bin/env bash
# Build the ICU gateway as a Greengrass v2 component and deploy it to the core device (thing icu-gw-01).
#   edge/icu-gateway/greengrass/deploy.sh            # uses terraform outputs from infra/terraform/aws
# Artifact: an offline wheelhouse for linux/aarch64 + CPython 3.12, so the core installs without internet access to
# PyPI. The component runs as ggc_user, publishes through the nucleus (IPC), and keeps its outbox in {work:path}.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
TF="$ROOT/infra/terraform/aws"
out() { terraform -chdir="$TF" output -raw "$1"; }
REGION="${AWS_REGION:-$(out region 2>/dev/null || echo ap-southeast-2)}"
BUCKET="$(out data_bucket)"
THING="$(out icu_gateway_id)"
VERSION="${GG_COMPONENT_VERSION:-0.3.$(date +%s)}"   # immutable component versions: bump every deploy
COMPONENT=com.cloudbox.rpm.IcuGateway
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

echo "building wheelhouse (linux/arm64, cp312)"
mkdir -p "$WORK/rpm-icu-gateway/wheels"
docker run --rm --platform linux/arm64 -v "$ROOT:/src:ro" -v "$WORK/rpm-icu-gateway/wheels:/wheels" \
  python:3.12-slim sh -c 'pip wheel --quiet --wheel-dir /wheels \
    /src/libs/rpm_contracts /src/libs/rpm_features "/src/edge/icu-gateway[greengrass]"'
(cd "$WORK" && zip -qr "rpm-icu-gateway-$VERSION.zip" rpm-icu-gateway)
KEY="greengrass/$COMPONENT/$VERSION/rpm-icu-gateway.zip"
aws s3 cp --only-show-errors "$WORK/rpm-icu-gateway-$VERSION.zip" "s3://$BUCKET/$KEY"

sed -e "s|__VERSION__|$VERSION|" -e "s|__ARTIFACT_URI__|s3://$BUCKET/$KEY|" \
  "$ROOT/edge/icu-gateway/greengrass/recipe.yaml" > "$WORK/recipe.yaml"
aws greengrassv2 create-component-version --region "$REGION" --inline-recipe "fileb://$WORK/recipe.yaml" \
  --query 'status.componentState' --output text

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
TARGET="arn:aws:iot:$REGION:$ACCOUNT:thing/$THING"
cat > "$WORK/deployment.json" <<JSON
{
  "targetArn": "$TARGET",
  "deploymentName": "rpm-icu-gateway-$VERSION",
  "components": {
    "aws.greengrass.Nucleus": {
      "componentVersion": "$(aws greengrassv2 list-component-versions --region "$REGION" \
        --arn "arn:aws:greengrass:$REGION:aws:components:aws.greengrass.Nucleus" \
        --query 'componentVersions[0].componentVersion' --output text)",
      "configurationUpdate": {"merge": "{\"interpolateComponentConfiguration\": true}"}
    },
    "$COMPONENT": {"componentVersion": "$VERSION"}
  },
  "deploymentPolicies": {"failureHandlingPolicy": "ROLLBACK", "componentUpdatePolicy": {"action": "NOTIFY_COMPONENTS", "timeoutInSeconds": 60}}
}
JSON
aws greengrassv2 create-deployment --region "$REGION" --cli-input-json "file://$WORK/deployment.json" \
  --query deploymentId --output text
echo "deployed $COMPONENT $VERSION to $THING; watch: aws greengrassv2 list-effective-deployments --core-device-thing-name $THING"
