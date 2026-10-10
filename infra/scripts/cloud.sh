#!/usr/bin/env bash
# The AWS deployment, one sub-command per task. Use it through make (`make help` lists the cloud-* targets).
#
#   up        create or update the CloudFormation stack, then wait until Kubernetes on the instance is ready
#   data      upload the training data, model plug-ins and protocols; store the care team's contacts
#   deploy    build the images, push them, and apply the Kubernetes manifests on the instance
#   seed      load the cohort (registry, FHIR record, guideline index, FHIR subscriptions)
#   status | logs <deployment> [lines] | fhir <query> | sim-pause | sim-resume
#   stop | start        stop the instance between demonstrations (data is kept) and start it again
#   user <username> <email> "<groups>"      create a console account (groups: physician nurse admin presenter)
#   device <id> [--sim]                     create a device certificate in .secrets/devices/<id>/
#   sim <rpm-sim arguments>                 run the simulator CLI on this machine against AWS IoT Core
#   e2e       the end-to-end check against the deployment
#   down      delete everything (needs CONFIRM=rpm)
#
# Nothing here uses SSH: the instance is administered through Systems Manager Run Command with your AWS sign-in.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH"
export AWS_PROFILE="${AWS_PROFILE:-rpm-capstone}" AWS_REGION=ap-southeast-2 AWS_DEFAULT_REGION=ap-southeast-2 AWS_PAGER=""
STACK=rpm
TEMPLATE=infra/cloudformation/rpm.yaml
INSTANCE_TYPE="${INSTANCE_TYPE:-m7i-flex.large}"
DATA_DIR="${DATA:-Training Data}"
SECRETS="$ROOT/.secrets"
KUBE='export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; K=/usr/local/bin/kubectl'
WORK="$(mktemp -d)"; chmod 700 "$WORK"; trap 'rm -rf "$WORK"' EXIT

say() { printf '\n== %s\n' "$*"; }
die() { echo "error: $*" >&2; exit 1; }

need_login() {
  aws sts get-caller-identity >/dev/null 2>&1 || die "your AWS sign-in has expired: run 'aws login --profile $AWS_PROFILE'"
}

stack_exists() { aws cloudformation describe-stacks --stack-name "$STACK" >/dev/null 2>&1; }

out() {  # a stack output, by key
  [[ -f "$WORK/outputs" ]] || aws cloudformation describe-stacks --stack-name "$STACK" \
    --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output text > "$WORK/outputs" 2>/dev/null \
    || die "the stack does not exist yet: run 'make cloud-up'"
  awk -v k="$1" '$1 == k { print $2 }' "$WORK/outputs"
}

dotenv() {  # a value from the git-ignored deploy/compose/.env
  [[ -f deploy/compose/.env ]] && sed -n "s/^$1=//p" deploy/compose/.env | tail -1 || true
}

param() { aws ssm get-parameter --name "$1" --with-decryption --query Parameter.Value --output text 2>/dev/null; }

put_param() {  # name, type, file (the value never appears on a command line)
  aws ssm put-parameter --name "$1" --type "$2" --value "file://$3" --overwrite >/dev/null
}

# Secrets the containers need live in Parameter Store (encrypted, free), never in git, the image or the template.
# Generated once; the alert sender is refreshed from deploy/compose/.env on every run.
sync_app_env() {
  local current sender token dbpw origin
  current="$(param /rpm/app-env || true)"
  token="$(sed -n 's/^RPM_SERVICE_TOKEN=//p' <<<"$current")"
  dbpw="$(sed -n 's/^POSTGRES_PASSWORD=//p' <<<"$current")"
  origin="$(sed -n 's/^ORIGIN_SECRET=//p' <<<"$current")"
  sender="$(dotenv RPM_EMAIL_SENDER)"
  [[ -n "$sender" ]] || die "set RPM_EMAIL_SENDER=<verified sender address> in deploy/compose/.env"
  : "${token:=$(openssl rand -hex 24)}" "${dbpw:=$(openssl rand -hex 16)}" "${origin:=$(openssl rand -hex 24)}"
  ( umask 077; cat > "$WORK/app-env" <<EOF
RPM_SERVICE_TOKEN=$token
POSTGRES_PASSWORD=$dbpw
RPM_DATABASE_URL=postgresql://rpm:$dbpw@postgres:5432/rpm
ORIGIN_SECRET=$origin
RPM_EMAIL_SENDER=$sender
EOF
  )
  [[ "$(cat "$WORK/app-env")" == "$current" ]] || put_param /rpm/app-env SecureString "$WORK/app-env"
  ORIGIN_SECRET="$origin"
}

ssm_run() {  # run a shell script on the instance as root and print its output; fails if the script fails
  local script="$1" timeout="${2:-600}" iid id status
  iid="$(out InstanceId)"
  id="$(aws ssm send-command --instance-ids "$iid" --document-name AWS-RunShellScript --comment rpm \
        --parameters "$(python3 -c 'import json,sys; print(json.dumps({"commands": [sys.argv[1]], "executionTimeout": [sys.argv[2]]}))' "$script" "$timeout")" \
        --query Command.CommandId --output text)"
  while :; do
    sleep 4
    status="$(aws ssm get-command-invocation --command-id "$id" --instance-id "$iid" --query Status --output text 2>/dev/null || echo Pending)"
    case "$status" in Pending|InProgress|Delayed) ;; *) break ;; esac
  done
  aws ssm get-command-invocation --command-id "$id" --instance-id "$iid" --query StandardOutputContent --output text
  if [[ "$status" != Success ]]; then
    aws ssm get-command-invocation --command-id "$id" --instance-id "$iid" --query StandardErrorContent --output text >&2
    echo "remote command ended with status $status" >&2
    return 1
  fi
}

wait_for_node() {
  local iid; iid="$(out InstanceId)"
  say "Waiting for the instance to register with Systems Manager"
  until [[ "$(aws ssm describe-instance-information --filters "Key=InstanceIds,Values=$iid" \
              --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null)" == Online ]]; do sleep 6; done
  say "Waiting for Kubernetes (k3s) on the instance"
  ssm_run "$KUBE; for i in \$(seq 1 90); do \$K get nodes 2>/dev/null | grep -q ' Ready' && break; sleep 5; done; \$K get nodes" 600
}

# ---------------------------------------------------------------------------------------------- up
cmd_up() {
  need_login
  sync_app_env
  local params=("InstanceType=$INSTANCE_TYPE" "OriginSecret=$ORIGIN_SECRET"
                "BudgetEmail=${RPM_BUDGET_EMAIL-$(dotenv RPM_EMAIL_SENDER)}")
  params+=("CloudFrontPrefixListId=$(aws ec2 describe-managed-prefix-lists \
      --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing \
      --query 'PrefixLists[0].PrefixListId' --output text)")
  if ! stack_exists; then   # the machine image is chosen once; later updates keep it, so the instance is never replaced
    local arch; arch="$(aws ec2 describe-instance-types --instance-types "$INSTANCE_TYPE" \
      --query 'InstanceTypes[0].ProcessorInfo.SupportedArchitectures[0]' --output text)"
    params+=("ImageId=$(aws ssm get-parameter --name "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-$arch" \
      --query Parameter.Value --output text)")
    say "Creating the stack (about 6 minutes; CloudFront is the slow part)"
  else
    say "Updating the stack"
  fi
  if ! aws cloudformation deploy --stack-name "$STACK" --template-file "$TEMPLATE" --capabilities CAPABILITY_NAMED_IAM \
        --no-fail-on-empty-changeset --tags project=rpm --parameter-overrides "${params[@]}"; then
    aws cloudformation describe-stack-events --stack-name "$STACK" --max-items 40 \
      --query "StackEvents[?contains(ResourceStatus,'FAILED')].[LogicalResourceId,ResourceStatusReason]" --output text >&2
    die "the stack did not deploy"
  fi
  rm -f "$WORK/outputs"
  wait_for_node
  say "Stack ready"
  cat "$WORK/outputs"
}

# ---------------------------------------------------------------------------------------------- data
cmd_data() {
  need_login
  local bucket; bucket="$(out ArtifactsBucket)"
  [[ -f "$DATA_DIR/rpm_training_sample.csv" ]] || die "the training data is not in '$DATA_DIR'"
  ls models/plugins/*/*/model.* >/dev/null 2>&1 || die "no fitted models: run 'make models' first"
  say "Uploading the training data, model plug-ins and protocols to s3://$bucket (private, encrypted)"
  aws s3 sync "$DATA_DIR" "s3://$bucket/training/" --delete --exclude '*' --include '*.csv' --only-show-errors
  aws s3 sync models/plugins "s3://$bucket/models/plugins/" --delete --exclude '*__pycache__*' --only-show-errors
  aws s3 sync knowledge/protocols "s3://$bucket/knowledge/protocols/" --delete --only-show-errors
  local team=deploy/config/care-team.yaml
  [[ -f "$team" ]] || { team=deploy/config/care-team.example.yaml; echo "warning: no care-team.yaml; using the example contacts"; }
  put_param /rpm/care-team SecureString "$team"
  echo "care team contacts stored from $team"
}

ensure_sim_fleet() {  # the certificate the simulated devices on the instance use; the key never touches git or S3
  aws ssm get-parameter --name /rpm/sim-fleet/key >/dev/null 2>&1 && return 0
  say "Creating the simulated fleet's certificate in AWS IoT Core"
  local arn
  arn="$(umask 077; aws iot create-keys-and-certificate --set-as-active --certificate-pem-outfile "$WORK/sim.cert" \
         --private-key-outfile "$WORK/sim.key" --query certificateArn --output text)"
  aws iot attach-policy --policy-name rpm-simulator --target "$arn"
  put_param /rpm/sim-fleet/cert String "$WORK/sim.cert"
  put_param /rpm/sim-fleet/key SecureString "$WORK/sim.key"
}

# ---------------------------------------------------------------------------------------------- deploy
build_images() {
  local registry arch platform
  registry="$(out PlatformRepository)"; registry="${registry%%/*}"
  arch="$(aws ec2 describe-instances --instance-ids "$(out InstanceId)" \
          --query 'Reservations[0].Instances[0].Architecture' --output text)"
  platform="linux/$([[ "$arch" == arm64 ]] && echo arm64 || echo amd64)"
  TAG="$(git rev-parse --short HEAD)-$(date +%Y%m%d%H%M)"
  say "Building images for $platform and pushing them as $TAG"
  aws ecr get-login-password | docker login --username AWS --password-stdin "$registry" >/dev/null
  docker buildx build --platform "$platform" --provenance=false -f deploy/docker/python.Dockerfile \
    -t "$(out PlatformRepository):$TAG" --push .
  docker buildx build --platform "$platform" --provenance=false -f deploy/docker/console.Dockerfile \
    -t "$(out ConsoleRepository):$TAG" --push .
}

cmd_deploy() {
  need_login
  sync_app_env
  cmd_data
  ensure_sim_fleet
  if [[ -n "${SKIP_BUILD:-}" ]]; then   # configuration-only change: reuse the newest image
    TAG="$(aws ecr describe-images --repository-name rpm/platform \
           --query 'sort_by(imageDetails,&imagePushedAt)[-1].imageTags[0]' --output text)"
    [[ "$TAG" != None ]] || die "no image has been pushed yet; run without SKIP_BUILD"
  else
    build_images
  fi
  local bucket rel="$WORK/release"; bucket="$(out ArtifactsBucket)"
  mkdir -p "$rel"
  cp -R deploy/k8s "$rel/k8s"
  cp deploy/compose/postgres/01-databases.sql "$rel/k8s/"
  cp infra/scripts/node-apply.sh "$rel/apply.sh"
  cat > "$rel/k8s/config.env" <<EOF
RPM_ENV=aws
RPM_LOG_LEVEL=${RPM_LOG_LEVEL:-INFO}
RPM_AWS_REGION=$AWS_REGION
AWS_REGION=$AWS_REGION
AWS_DEFAULT_REGION=$AWS_REGION
RPM_FHIR_BASE_URL=http://fhir:8080/fhir
RPM_CARE_API_URL=http://care-api:8000
RPM_RISK_SERVICE_URL=http://risk-service:8001
RPM_COHORT_ICU=${RPM_COHORT_ICU:-6}
RPM_COHORT_HOME=${RPM_COHORT_HOME:-24}
RPM_AUTH_MODE=oidc
RPM_OIDC_ISSUER=$(out OidcIssuer)
RPM_OIDC_CLIENT_ID=$(out ClientId)
RPM_OIDC_DOMAIN=$(out AuthDomain)
AUTH_ORIGIN=$(out AuthDomain)
RPM_DEVICE_COMMAND_MODE=iot
RPM_IOT_ENDPOINT=$(aws iot describe-endpoint --endpoint-type iot:Data-ATS --query endpointAddress --output text)
RPM_EMAIL_BACKEND=ses
RPM_CARE_TEAM_FILE=/config/care-team.yaml
RPM_CONSOLE_URL=$(out ConsoleUrl)
RPM_ESCALATION_TIMEOUT_SCALE=${RPM_ESCALATION_TIMEOUT_SCALE:-1}
RPM_MAX_REMINDERS=${RPM_MAX_REMINDERS:-1}
RPM_AGENT_MONITOR_INTERVAL_S=${RPM_AGENT_MONITOR_INTERVAL_S:-3600}
RPM_AGENT_DAILY_LIMIT=${RPM_AGENT_DAILY_LIMIT:-80}
RPM_EMBEDDINGS=titan
RPM_LLM_PROVIDER=${RPM_LLM_PROVIDER:-bedrock}
RPM_MODELS_URI=s3://$bucket/models/plugins
RPM_DATA_DIR=s3://$bucket/training
RPM_PROTOCOLS_DIR=s3://$bucket/knowledge/protocols
RPM_AUDIT_DEST=s3://$(out AuditBucket)/app-audit
EOF
  cat >> "$rel/k8s/kustomization.yaml" <<EOF
images:
  - {name: rpm/platform, newName: $(out PlatformRepository), newTag: "$TAG"}
  - {name: rpm/console, newName: $(out ConsoleRepository), newTag: "$TAG"}
EOF
  echo "REGISTRY=$(out PlatformRepository | cut -d/ -f1)" > "$rel/release.env"
  COPYFILE_DISABLE=1 tar -czf "$WORK/release.tgz" -C "$rel" .
  aws s3 cp "$WORK/release.tgz" "s3://$bucket/releases/$TAG.tgz" --only-show-errors
  say "Applying release $TAG on the instance"
  ssm_run "set -e; rm -rf /opt/rpm/release; mkdir -p /opt/rpm/release; \
aws s3 cp s3://$bucket/releases/$TAG.tgz /opt/rpm/release.tgz --only-show-errors; \
tar -xzf /opt/rpm/release.tgz -C /opt/rpm/release; bash /opt/rpm/release/apply.sh" 1500
  echo "console: $(out ConsoleUrl)"
}

# ---------------------------------------------------------------------------------------------- seed and operations
cmd_seed() {
  need_login
  say "Loading the cohort on the instance (registry, FHIR record, guideline index, subscriptions)"
  ssm_run "$KUBE; J=seed-\$(date +%s); \$K -n rpm create job \$J --from=cronjob/seed >/dev/null
for i in \$(seq 1 180); do
  S=\$(\$K -n rpm get job \$J -o jsonpath='{.status.succeeded}{\"/\"}{.status.failed}')
  [ \"\$S\" != / ] && break; sleep 5
done
\$K -n rpm logs job/\$J --tail 25 | grep -v '^{' || true
[ \"\${S%%/*}\" = 1 ] || { echo 'seed failed'; \$K -n rpm logs job/\$J --tail 30; exit 1; }
\$K -n rpm rollout restart deployment/simulator >/dev/null; echo 'simulated devices restarted'" 1200
}

cmd_status() {
  need_login
  cat <<EOF
console     $(out ConsoleUrl)
instance    $(out InstanceId) ($(aws ec2 describe-instances --instance-ids "$(out InstanceId)" \
              --query 'Reservations[0].Instances[0].[InstanceType,State.Name]' --output text | tr '\t' ' '))
EOF
  ssm_run "$KUBE; \$K -n rpm get pods -o wide 2>&1 | cut -c1-110; echo; \$K top pods -n rpm 2>&1 | head -20; echo; free -m | head -2; df -h / | tail -1" 120
}

cmd_logs() {
  need_login
  ssm_run "$KUBE; \$K -n rpm logs deployment/${1:?which deployment? care-api, stream-processor, risk-service, agent-service, simulator, fhir, console} --tail ${2:-60} 2>&1 | cut -c1-600" 120
}

cmd_fhir() {  # the FHIR server is not reachable from the internet: query it from inside the cluster
  need_login
  local q; q="$(printf %s "${1:?a FHIR query, for example Patient/RPM-10001725}" | base64 | tr -d '\n')"
  ssm_run "$KUBE; \$K -n rpm exec deployment/care-api -- python -c \"import base64, urllib.request as u; print(u.urlopen('http://fhir:8080/fhir/' + base64.b64decode('$q').decode(), timeout=30).read().decode()[:20000])\"" 120
}

cmd_scale_sim() { need_login; ssm_run "$KUBE; \$K -n rpm scale deployment/simulator --replicas=$1" 120; }

cmd_stop() {
  need_login
  local iid; iid="$(out InstanceId)"
  aws ec2 stop-instances --instance-ids "$iid" >/dev/null
  aws ec2 wait instance-stopped --instance-ids "$iid"
  echo "instance stopped: compute charges have stopped; the disk and the address (about US\$0.25 a day) remain"
}

cmd_start() {
  need_login
  local iid; iid="$(out InstanceId)"
  aws ec2 start-instances --instance-ids "$iid" >/dev/null
  aws ec2 wait instance-running --instance-ids "$iid"
  wait_for_node
  echo "started; the containers come back by themselves within about three minutes: $(out ConsoleUrl)"
}

# ---------------------------------------------------------------------------------------------- people and devices
cmd_user() {
  need_login
  local user="${1:?username, for example ritik.patel}" email="${2:?e-mail address}" groups="${3:-physician}" pool
  pool="$(out UserPoolId)"
  aws cognito-idp admin-create-user --user-pool-id "$pool" --username "$user" --desired-delivery-mediums EMAIL \
    --user-attributes "Name=email,Value=$email" Name=email_verified,Value=true >/dev/null
  for g in $groups; do
    aws cognito-idp admin-add-user-to-group --user-pool-id "$pool" --username "$user" --group-name "$g"
  done
  echo "created $user in: $groups. A temporary password has been e-mailed to $email (valid 7 days)."
}

iot_files() {
  mkdir -p "$SECRETS/devices"; chmod 700 "$SECRETS" "$SECRETS/devices"
  [[ -f "$SECRETS/AmazonRootCA1.pem" ]] || curl -sf https://www.amazontrust.com/repository/AmazonRootCA1.pem \
    -o "$SECRETS/AmazonRootCA1.pem"
  aws iot describe-endpoint --endpoint-type iot:Data-ATS --query endpointAddress --output text > "$SECRETS/iot-endpoint.txt"
}

cmd_device() {
  need_login
  local id="${1:?device id, for example gw-demo-01}" sim="${2:-}" dir arn
  [[ "$id" =~ ^[A-Za-z0-9_-]{3,64}$ ]] || die "a device id is 3 to 64 letters, digits, - or _"
  dir="$SECRETS/devices/$id"
  [[ ! -f "$dir/key.pem" ]] || { echo "$id already has a certificate in $dir"; return 0; }
  iot_files
  mkdir -p "$dir"; chmod 700 "$dir"
  arn="$(umask 077; aws iot create-keys-and-certificate --set-as-active --certificate-pem-outfile "$dir/cert.pem" \
         --private-key-outfile "$dir/key.pem" --query certificateArn --output text)"
  if [[ "$sim" == --sim ]]; then
    aws iot attach-policy --policy-name rpm-simulator --target "$arn"
  else   # a real device: the certificate is tied to one thing, and the policy only allows that thing's topics
    aws iot describe-thing --thing-name "$id" >/dev/null 2>&1 || aws iot create-thing --thing-name "$id" >/dev/null
    aws iot attach-thing-principal --thing-name "$id" --principal "$arn"
    aws iot attach-policy --policy-name rpm-device --target "$arn"
  fi
  echo "certificate for $id written to $dir (git-ignored)"
}

cmd_sim() {
  need_login
  [[ -f "$SECRETS/devices/sim-mac/key.pem" ]] || cmd_device sim-mac --sim
  iot_files
  RPM_MQTT_HOST="$(cat "$SECRETS/iot-endpoint.txt")" RPM_MQTT_PORT=8883 RPM_MQTT_CLIENT_PREFIX=sim-mac \
  RPM_MQTT_TLS_CA="$SECRETS/AmazonRootCA1.pem" RPM_MQTT_TLS_CERT="$SECRETS/devices/sim-mac/cert.pem" \
  RPM_MQTT_TLS_KEY="$SECRETS/devices/sim-mac/key.pem" RPM_DATA_DIR="$DATA_DIR" uv run rpm-sim "$@"
}

cmd_e2e() {  # signs in as a throw-away account that exists only for the length of the run
  need_login
  local pool client user=e2e.runner pw token
  pool="$(out UserPoolId)"; client="$(out ClientId)"
  pw="$(openssl rand -hex 16)Aa1"
  aws cognito-idp admin-delete-user --user-pool-id "$pool" --username "$user" 2>/dev/null || true
  trap "aws cognito-idp admin-delete-user --user-pool-id '$pool' --username '$user' 2>/dev/null; rm -rf '$WORK'" EXIT
  aws cognito-idp admin-create-user --user-pool-id "$pool" --username "$user" --message-action SUPPRESS \
    --user-attributes Name=email,Value=e2e@rpm.invalid Name=email_verified,Value=true >/dev/null
  aws cognito-idp admin-set-user-password --user-pool-id "$pool" --username "$user" --password "$pw" --permanent
  for g in physician presenter admin; do
    aws cognito-idp admin-add-user-to-group --user-pool-id "$pool" --username "$user" --group-name "$g"
  done
  token="$(aws cognito-idp admin-initiate-auth --user-pool-id "$pool" --client-id "$client" \
           --auth-flow ADMIN_USER_PASSWORD_AUTH --auth-parameters "USERNAME=$user,PASSWORD=$pw" \
           --query AuthenticationResult.AccessToken --output text)"
  python3 scripts/e2e.py --api "$(out ConsoleUrl)" --fhir "" --mailpit "" --token "$token" "$@"
}

# ---------------------------------------------------------------------------------------------- down
cmd_down() {
  need_login
  [[ "${CONFIRM:-}" == "$STACK" ]] || die "this deletes the deployment and its data. Run: make cloud-down CONFIRM=$STACK"
  say "Removing device certificates"
  local policy arn thing id
  for policy in rpm-device rpm-simulator; do
    for arn in $(aws iot list-targets-for-policy --policy-name "$policy" --query 'targets[]' --output text 2>/dev/null); do
      aws iot detach-policy --policy-name "$policy" --target "$arn"
      for thing in $(aws iot list-principal-things --principal "$arn" --query 'things[]' --output text); do
        aws iot detach-thing-principal --thing-name "$thing" --principal "$arn"
        aws iot delete-thing --thing-name "$thing"
      done
      id="${arn##*/}"
      aws iot update-certificate --certificate-id "$id" --new-status INACTIVE
      aws iot delete-certificate --certificate-id "$id" --force-delete
    done
  done
  say "Emptying the buckets"
  aws s3 rm "s3://$(out ArtifactsBucket)" --recursive --only-show-errors || true
  python3 - "$(out AuditBucket)" <<'PY'
import json, subprocess, sys
bucket = sys.argv[1]
while True:
    v = json.loads(subprocess.run(["aws", "s3api", "list-object-versions", "--bucket", bucket, "--max-items", "500",
                                   "--output", "json"], capture_output=True, text=True, check=True).stdout or "{}")
    objs = [{"Key": o["Key"], "VersionId": o["VersionId"]} for k in ("Versions", "DeleteMarkers") for o in v.get(k) or []]
    if not objs:
        break
    subprocess.run(["aws", "s3api", "delete-objects", "--bucket", bucket, "--delete", json.dumps({"Objects": objs, "Quiet": True})],
                   check=True, capture_output=True)
PY
  say "Deleting the stack (about 5 minutes)"
  aws cloudformation delete-stack --stack-name "$STACK"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK"
  for p in /rpm/app-env /rpm/care-team /rpm/sim-fleet/cert /rpm/sim-fleet/key; do
    aws ssm delete-parameter --name "$p" 2>/dev/null || true
  done
  rm -rf "$SECRETS/devices" "$SECRETS/iot-endpoint.txt"
  echo "deleted. The verified e-mail addresses in Amazon SES are kept (they cost nothing)."
}

cmd="${1:-}"; shift || true
case "$cmd" in
  up) cmd_up ;;
  data) cmd_data ;;
  deploy) cmd_deploy ;;
  seed) cmd_seed ;;
  status) cmd_status ;;
  logs) cmd_logs "$@" ;;
  fhir) cmd_fhir "$@" ;;
  sim-pause) cmd_scale_sim 0 ;;
  sim-resume) cmd_scale_sim 1 ;;
  stop) cmd_stop ;;
  start) cmd_start ;;
  user) cmd_user "$@" ;;
  device) cmd_device "$@" ;;
  sim) cmd_sim "$@" ;;
  e2e) cmd_e2e "$@" ;;
  down) cmd_down ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac
