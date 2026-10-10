#!/usr/bin/env bash
# Provision a device identity into the watch app (or the optional phone companion) over adb.
#
#   edge/android/provision.sh plain [--host 10.0.2.2]      tcp://HOST:1883, no TLS (emulator dev loop)
#   edge/android/provision.sh local [--host 10.0.2.2]      mutual TLS to the local Mosquitto on 8883 with a dev CA,
#                                                          mirroring IoT Core (client cert required, topic ACL)
#   edge/android/provision.sh aws                          mutual TLS to AWS IoT Core with the certificate that
#                                                          `make cloud-device D=<id>` wrote to .secrets/devices/<id>/
# Options: --app wear|phone (default wear)  --device ID (default gw-demo-01)  --serial ADB_SERIAL
#
# Writes provisioning.json + device.p12 + ca.pem into the app's files directory (see rpm.edge.Provisioning) and
# restarts the app. Key material is streamed into the app sandbox with run-as; it never lands in a shared directory
# on the device. Needs a debuggable build for the watch. The production path is fleet provisioning by claim with
# the private key generated in the Android Keystore; see edge/android/README.md.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SECRETS="$ROOT/.secrets"
PKI="$SECRETS/dev-pki"
MODE="${1:-}"; shift || true
APP=wear; DEVICE=gw-demo-01; HOST=10.0.2.2; SERIAL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --app) APP="$2"; shift 2 ;;
    --device) DEVICE="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --serial) SERIAL="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
[[ "$MODE" =~ ^(plain|local|aws)$ ]] || { sed -n '2,15p' "$0"; exit 2; }
[[ "$APP" =~ ^(wear|phone)$ ]] || { echo "--app must be wear or phone" >&2; exit 2; }
PKG="com.cloudbox.rpm.$APP"
ADB=(adb); command -v adb >/dev/null || ADB=("$HOME/Library/Android/sdk/platform-tools/adb")
[[ -n "$SERIAL" ]] && ADB+=(-s "$SERIAL")

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
chmod 700 "$WORK"

# ---------------------------------------------------------------- dev PKI (local mode)
ensure_dev_pki() {
  mkdir -p "$PKI/mosquitto"; chmod 700 "$SECRETS" "$PKI"
  if [[ ! -f "$PKI/ca.pem" ]]; then
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 825 -subj "/CN=RPM dev CA" \
      -keyout "$PKI/ca.key" -out "$PKI/ca.pem" 2>/dev/null
    echo "created dev CA $PKI/ca.pem"
  fi
  local lan; lan="$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || true)"
  local san="DNS:localhost,DNS:mosquitto,IP:127.0.0.1,IP:10.0.2.2${lan:+,IP:$lan}"
  if [[ ! -f "$PKI/mosquitto/server.pem" ]] || ! openssl x509 -in "$PKI/mosquitto/server.pem" -noout -ext subjectAltName 2>/dev/null | grep -q "${lan:-127.0.0.1}"; then
    openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=mosquitto" \
      -keyout "$PKI/mosquitto/server.key" -out "$WORK/server.csr" 2>/dev/null
    openssl x509 -req -in "$WORK/server.csr" -CA "$PKI/ca.pem" -CAkey "$PKI/ca.key" -CAcreateserial -days 825 \
      -extfile <(printf "subjectAltName=%s\nextendedKeyUsage=serverAuth" "$san") -out "$PKI/mosquitto/server.pem" 2>/dev/null
    chmod 644 "$PKI/mosquitto/server.key"   # read by the broker's own uid inside the container; dev only
    echo "issued broker certificate for $san"
  fi
  cp "$PKI/ca.pem" "$PKI/mosquitto/ca.pem"
  cat > "$PKI/mosquitto/acl" <<'ACL'
# Mirrors the IoT Core device policy: a device may only use its own topics (%u = certificate CN = thing name).
pattern write rpm/v1/dev/%u/telemetry
pattern write rpm/v1/dev/%u/events
pattern read rpm/v1/dev/%u/cmd
pattern read rpm/v1/dev/%u/config
ACL
  cat > "$PKI/mosquitto/tls.conf" <<'CONF'
# Written by edge/android/provision.sh local. Dev mutual-TLS listener that behaves like AWS IoT Core.
listener 8883 0.0.0.0
cafile /mosquitto/config/conf.d/ca.pem
certfile /mosquitto/config/conf.d/server.pem
keyfile /mosquitto/config/conf.d/server.key
tls_version tlsv1.2
require_certificate true
use_identity_as_username true
use_username_as_clientid true
allow_anonymous false
acl_file /mosquitto/config/conf.d/acl
CONF
  if [[ ! -f "$PKI/$DEVICE.cert.pem" ]]; then
    openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=$DEVICE" \
      -keyout "$PKI/$DEVICE.key.pem" -out "$WORK/device.csr" 2>/dev/null
    openssl x509 -req -in "$WORK/device.csr" -CA "$PKI/ca.pem" -CAkey "$PKI/ca.key" -CAcreateserial -days 825 \
      -extfile <(printf "extendedKeyUsage=clientAuth") -out "$PKI/$DEVICE.cert.pem" 2>/dev/null
    chmod 600 "$PKI/$DEVICE.key.pem"
    echo "issued device certificate CN=$DEVICE"
  fi
  docker compose -f "$ROOT/deploy/compose/docker-compose.yml" restart mosquitto >/dev/null
  echo "mosquitto restarted with the mTLS listener on :8883"
}

case "$MODE" in
  plain)
    URI="tcp://$HOST:1883" ;;
  local)
    ensure_dev_pki
    URI="ssl://$HOST:8883"; CERT="$PKI/$DEVICE.cert.pem"; KEY="$PKI/$DEVICE.key.pem"; CA="$PKI/ca.pem" ;;
  aws)
    CERT="$SECRETS/devices/$DEVICE/cert.pem"; KEY="$SECRETS/devices/$DEVICE/key.pem"
    CA="$SECRETS/AmazonRootCA1.pem"
    for f in "$CERT" "$KEY" "$CA" "$SECRETS/iot-endpoint.txt"; do
      [[ -f "$f" ]] || { echo "missing $f: run 'make cloud-device D=$DEVICE' first" >&2; exit 1; }
    done
    URI="ssl://$(cat "$SECRETS/iot-endpoint.txt"):8883" ;;
esac

# ---------------------------------------------------------------- bundle
if [[ "$MODE" == plain ]]; then
  printf '{"mqtt_uri":"%s","device_id":"%s"}\n' "$URI" "$DEVICE" > "$WORK/provisioning.json"
  FILES=(provisioning.json)
else
  PASS="$(openssl rand -hex 16)"
  # PBE-SHA1-3DES keeps the bundle loadable by the PKCS#12 KeyStore on every Wear OS 3+/Android 8+ device. The
  # password only satisfies the KeyStore API: it sits next to the file, so the app sandbox is the real protection.
  openssl pkcs12 -export -inkey "$KEY" -in "$CERT" -name "$DEVICE" -out "$WORK/device.p12" -passout "pass:$PASS" \
    -keypbe PBE-SHA1-3DES -certpbe PBE-SHA1-3DES -macalg sha1
  cp "$CA" "$WORK/ca.pem"
  printf '{"mqtt_uri":"%s","device_id":"%s","p12_password":"%s"}\n' "$URI" "$DEVICE" "$PASS" > "$WORK/provisioning.json"
  FILES=(provisioning.json device.p12 ca.pem)
fi

# ---------------------------------------------------------------- push + restart
"${ADB[@]}" get-state >/dev/null
if [[ "$APP" == wear ]]; then
  "${ADB[@]}" shell run-as "$PKG" true 2>/dev/null || { echo "$PKG is not installed as a debuggable build" >&2; exit 1; }
  "${ADB[@]}" shell run-as "$PKG" sh -c "'mkdir -p files && rm -f files/provisioning.json files/device.p12 files/ca.pem'"
  for f in "${FILES[@]}"; do
    "${ADB[@]}" exec-in "run-as $PKG sh -c 'umask 077; cat > files/$f'" < "$WORK/$f"   # one string: exec-in runs it verbatim
  done
  for f in "${FILES[@]}"; do   # exec-in does not report remote failures; verify every file landed intact
    [[ "$("${ADB[@]}" exec-out run-as "$PKG" cat "files/$f" | openssl dgst -sha256 -r | cut -d' ' -f1)" == \
       "$(openssl dgst -sha256 -r < "$WORK/$f" | cut -d' ' -f1)" ]] || { echo "failed to write files/$f" >&2; exit 1; }
  done
  "${ADB[@]}" shell am force-stop "$PKG"
  "${ADB[@]}" shell am start -n "$PKG/.MainActivity" --es transport direct >/dev/null
else
  DIR="/sdcard/Android/data/$PKG/files"
  "${ADB[@]}" shell mkdir -p "$DIR"
  "${ADB[@]}" shell rm -f "$DIR/provisioning.json" "$DIR/device.p12" "$DIR/ca.pem"
  for f in "${FILES[@]}"; do "${ADB[@]}" push -q "$WORK/$f" "$DIR/$f" >/dev/null; done
  "${ADB[@]}" shell am force-stop "$PKG"
  "${ADB[@]}" shell am start -n "$PKG/.MainActivity" >/dev/null
fi
echo "provisioned $PKG as $DEVICE -> $URI"
