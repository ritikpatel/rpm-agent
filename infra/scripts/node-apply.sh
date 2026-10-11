#!/bin/bash
# Runs on the AWS instance, as root, through Systems Manager (`make cloud-deploy` sends it inside the release bundle).
# Turns the bundle in this directory into running containers:
#   1. fetch the secrets from Parameter Store with the instance's own role (they are never in the bundle or in git)
#   2. refresh the image pull secret for Amazon ECR (and install a timer that keeps it fresh)
#   3. kubectl apply -k, then wait for every workload to be ready
set -euo pipefail
cd "$(dirname "$0")"
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml PATH="$PATH:/usr/local/bin"
. ./release.env
AWS_DEFAULT_REGION="$(sed -n 's/^AWS_REGION=//p' k8s/config.env)"; export AWS_DEFAULT_REGION
NS=rpm
umask 077

cleanup() { rm -f k8s/secrets.env k8s/care-team.yaml k8s/sim-cert.pem k8s/sim-key.pem k8s/ingress.yaml; }
trap cleanup EXIT

param() { aws ssm get-parameter --name "$1" --with-decryption --query Parameter.Value --output text; }
param /rpm/app-env > k8s/secrets.env
param /rpm/care-team > k8s/care-team.yaml
param /rpm/sim-fleet/cert > k8s/sim-cert.pem
param /rpm/sim-fleet/key > k8s/sim-key.pem
origin="$(sed -n 's/^ORIGIN_SECRET=//p' k8s/secrets.env)"
sed -i '/^ORIGIN_SECRET=/d' k8s/secrets.env
sed -i "s/__ORIGIN_SECRET__/$origin/" k8s/ingress.yaml

echo "waiting for the ingress controller"
for _ in $(seq 1 60); do kubectl get crd ingressroutes.traefik.io >/dev/null 2>&1 && break; sleep 5; done
kubectl get crd ingressroutes.traefik.io >/dev/null

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

# Amazon ECR passwords last 12 hours; images are only pulled on a new release, but keep the secret fresh anyway.
cat > /usr/local/bin/rpm-ecr-login <<EOF
#!/bin/bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
/usr/local/bin/kubectl -n $NS create secret docker-registry ecr --docker-server=$REGISTRY --docker-username=AWS \\
  --docker-password="\$(aws ecr get-login-password --region $AWS_DEFAULT_REGION)" --dry-run=client -o yaml \\
  | /usr/local/bin/kubectl apply -f - >/dev/null
EOF
chmod 700 /usr/local/bin/rpm-ecr-login
cat > /etc/systemd/system/rpm-ecr-login.service <<'EOF'
[Unit]
Description=Refresh the Amazon ECR image pull secret
After=k3s.service
[Service]
Type=oneshot
ExecStart=/usr/local/bin/rpm-ecr-login
EOF
cat > /etc/systemd/system/rpm-ecr-login.timer <<'EOF'
[Unit]
Description=Refresh the Amazon ECR image pull secret every 6 hours
[Timer]
OnBootSec=3min
OnUnitActiveSec=6h
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now rpm-ecr-login.timer >/dev/null 2>&1
systemctl start rpm-ecr-login.service   # through systemd, so it never runs twice at once with the timer

# `make cloud-sim-pause` survives a release: if the simulated fleet is paused, this release asks for zero copies of
# it too, so no device connects even for a moment.
if [ "$(kubectl -n "$NS" get deployment simulator -o jsonpath='{.spec.replicas}' 2>/dev/null || true)" = 0 ]; then
  printf 'replicas:\n  - {name: simulator, count: 0}\n' >> k8s/kustomization.yaml
  echo "simulated devices stay paused (make cloud-sim-resume starts them)"
fi
kubectl apply -k k8s | sed 's/^/  /'

fail() {
  echo "NOT READY: $1"
  kubectl -n "$NS" get pods
  kubectl -n "$NS" describe "$1" | tail -15
  kubectl -n "$NS" logs "$1" --tail 30 2>&1 | cut -c1-400 || true
  exit 1
}
kubectl -n "$NS" rollout status statefulset/postgres --timeout=300s >/dev/null || fail statefulset/postgres
for d in care-api risk-service stream-processor agent-service console simulator; do
  kubectl -n "$NS" rollout status "deployment/$d" --timeout=420s >/dev/null || fail "deployment/$d"
done
kubectl -n "$NS" rollout status deployment/fhir --timeout=720s >/dev/null || fail deployment/fhir
echo
kubectl -n "$NS" get pods
