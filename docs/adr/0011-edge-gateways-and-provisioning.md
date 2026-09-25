# ADR 0011 — Edge gateways, relayed identity and device provisioning

**Status:** accepted · 2026-09-25 (refines ADR 0005)

## ICU bedside gateway

- **Protocol at the bedside:** bedside monitors speak HL7 v2 (ORU^R01 over MLLP), not MQTT. The gateway accepts
  IEEE 11073 MDC codes and LOINC, normalises units, drops unusable observations (status X/D, implausible values),
  and ACKs every message (AA / AE / AR), so monitors retransmit only what really failed.
- **Relayed identity:**
  - The gateway is one IoT thing (`icu-gw-01`) that publishes on its own topics on behalf of its monitors.
  - `device_assignments.gateway_id` names the only thing allowed to publish for a device other than the device
    itself.
  - The stream engine still rejects every other mismatch between topic and payload (ADR 0005).
  - Commands for a relayed device go to the gateway's topic, with `args.device_id` naming the bed.
- **PHI:** PID segments are counted and never forwarded. Beds are keyed by monitor id, exactly like watches.
- **Bedside NEWS2 and alarms:**
  - These run on the gateway, so an ICU alarm still sounds when the cloud link is down.
  - Alarms latch, and are re-announced every 10 minutes while active.
  - The cloud recomputes NEWS2 from the telemetry with the same library (`rpm_features`). The bedside score travels
    in `edge.flags` so the two can be compared.
- **Per-bed clinical context comes from the cloud** (`rpm.gateway_config.v1`, retained on
  `rpm/v1/dev/{gateway}/config`): the SpO₂ scale (Scale 2 for hypercapnic failure), supplemental O₂, and the
  episode's best GCS.
  - Without it, bedside and cloud NEWS2 disagreed on 3 of 23 windows. COPD beds scored 3 points too high, which
    would have opened false alerts through `EDGE_ALERT`. A gateway that joined mid-episode missed new confusion.
  - With it, all windows agreed except one, which was scored 0.5 s before the config arrived.
  - The config and the GCS baselines persist in the gateway's SQLite store and survive restarts.
- **Store and forward:**
  - The SQLite outbox sends events first, then telemetry FIFO. The oldest telemetry is dropped first; events are
    never dropped.
  - Sequence numbers are monotonic per bed across restarts, so the cloud's gap detection stays valid.
  - Anything older than 60 s is flagged `buffered`, so the cloud treats it as backfill and it is excluded from live
    latency SLOs.
- **Packaging:** the same Python package runs in two forms.
  - **Greengrass v2 component** (`com.cloudbox.rpm.IcuGateway`): an offline arm64 wheelhouse, with IPC
    publish/subscribe scoped to the core's own topics. The nucleus owns the mTLS session and spools offline.
    Terraform creates it with `enable_greengrass_core`: a t4g.small core, a token-exchange role alias, and the
    identity delivered through Secrets Manager.
  - **Plain container** (compose, and the EKS default): its own mTLS MQTT session. This was the fallback the plan
    allowed for, and it also keeps the demo cheap.

## Phone and watch provisioning

- `edge/android/provision.sh` writes `provisioning.json`, `device.p12` and `ca.pem` into the app's private
  storage (`run-as`, mode 0600, verified by checksum). `rpm.edge.Provisioning` refuses an `ssl://` endpoint without
  valid key material; it never falls back to an unauthenticated connection.
- A local mTLS broker mirrors IoT Core: a dev CA, a required client certificate, the client id forced to the
  certificate CN, and a per-device topic ACL.
  - Verified on the Wear OS emulator: TLS 1.2 with ECDHE-ECDSA-AES256-GCM, connected as `gw-demo-01`.
  - A client with no certificate is rejected at the handshake.
  - A publish to another device's topic is dropped.
- **Not done (demo shortcut):** fleet provisioning by claim with an Android-Keystore key. Keys are still generated
  by AWS (Terraform) and moved by adb. The production path is described in `edge/android/README.md`.

## Consequences

- Adding a new kind of bedside device means adding a code mapping, not a new cloud integration.
- The registry is the single source of truth for which gateway relays which bed. Moving a bed means re-running
  the registry step, which republishes the retained config.
