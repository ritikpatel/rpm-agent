# Android edge: Galaxy Watch app + phone gateway

Two apps and a shared, pure-Kotlin core:

| Module | What it is |
|---|---|
| `shared/` | Edge logic with no Android dependency, unit-tested on the JVM. Covers the contracts (`rpm.telemetry.v1`, `rpm.device_event.v1`, `rpm.command.v1`), windowing plus signal quality, the edge rules, the outbox, the uplinker (seq, `edge_sent`, `buffered`), the MQTT transport, mTLS and provisioning. |
| `wear/` | Wear OS app (`com.cloudbox.rpm.wear`). A foreground health service reads sensors, runs the edge pipeline, alerts the patient with haptics, and uplinks either **directly** over MQTT or **through the phone**. The UI shows live HR/SpO₂, check-in questions, notices and a two-tap SOS. |
| `phone/` | Companion gateway (`com.cloudbox.rpm.phone`). It holds the IoT identity, keeps a durable SQLite outbox and relays cloud commands to the watch. It also has a patient screen for check-ins and manual BP or temperature readings. |

```
Galaxy Watch ──(Data Layer)──▶ phone gateway ──(MQTT, mTLS)──▶ AWS IoT Core   (companion mode)
Galaxy Watch / emulator ──────────(MQTT, mTLS)──────────────▶ AWS IoT Core   (direct mode, LTE/Wi-Fi watch)
```

## Sensor sources (chosen at start, shown on the watch)

| Source | When | Metrics |
|---|---|---|
| Samsung Health Sensor SDK | Galaxy Watch 4 or later, with the SDK `.aar` in `wear/libs/` | continuous HR + IBI (HRV), on-demand SpO₂, skin temperature |
| Health Services (`MeasureClient`) | any other Wear OS 3+ watch | HR |
| Scripted | the emulator, or `--es source scripted` | HR/HRV/SpO₂/skin temperature, following `normal` / `deteriorate` / `hypoxia` |

The Samsung SDK is licensed separately. Download it from Samsung Developers and put the `.aar` in `wear/libs/`. The build detects it and compiles `src/samsung/`. Without it, `src/nosamsung/` is used and the app still runs on Health Services.

## Build and test

```bash
cd edge/android
./gradlew :shared:test :wear:assembleDebug :phone:assembleDebug
```

This needs JDK 17+ and Android SDK 35; set `sdk.dir` in `local.properties`. The shared tests also check every emitted message against the JSON Schemas in `contracts/`, so contract drift fails the build.

## Run on the Wear OS emulator (no watch needed)

```bash
make up                                             # local stack
adb install -r wear/build/outputs/apk/debug/wear-debug.apk
adb shell pm grant com.cloudbox.rpm.wear android.permission.BODY_SENSORS
adb shell am start -n com.cloudbox.rpm.wear/.MainActivity \
  --es transport direct --es device_id gw-demo-01 --es source scripted
```

The emulator reaches the host's Mosquitto at `10.0.2.2`. `gw-demo-01` is mapped to the reserved demo patient (Daniel Haddad), so readings appear in the console under that patient. The scenario chips on the watch drive deterioration. In debug builds the same actions can be scripted from adb: `--es scenario hypoxia`, `--es action sos`, `--es answer Yes`.

## Provisioning a device identity

`provision.sh` writes `provisioning.json`, `device.p12` and `ca.pem` into the app's private files directory, then restarts the app. `rpm.edge.Provisioning` loads them. An `ssl://` endpoint with missing or invalid key material is a hard error; the app never falls back to an unauthenticated connection.

```bash
edge/android/provision.sh plain                 # tcp://10.0.2.2:1883, no TLS
edge/android/provision.sh local                 # mTLS to local Mosquitto :8883 with a dev CA (see below)
edge/android/provision.sh aws                   # mTLS to AWS IoT Core, cert from infra/scripts/cloud-up.sh
edge/android/provision.sh aws --app phone       # same identity into the phone gateway instead
```

**`local` mirrors IoT Core** so the TLS path can be tested before any AWS spend:

- a dev CA, a broker certificate (SANs for `localhost`, `10.0.2.2` and the host's LAN IP), and a device certificate with CN = thing name;
- a Mosquitto listener that requires a client certificate and forces the client id to the certificate CN;
- a per-device ACL: a device may only publish to its own `telemetry`/`events` topics and read its own `cmd` topic.

We verified this on the emulator: the watch negotiated TLS 1.2 with ECDHE-ECDSA-AES256-GCM and was authenticated as `gw-demo-01`. A client with no certificate is rejected at the handshake. A publish to another device's topic is dropped, while one to the device's own topic is delivered.

**Demo shortcut vs production.** In the demo, Terraform creates the key pair, `cloud-up.sh` writes it to the git-ignored `.secrets/`, and this script moves it to the device over adb. The PKCS#12 password is stored next to the bundle (the KeyStore API requires one), so the app sandbox is the actual protection. Production would use **fleet provisioning by claim**:

1. The app generates a non-exportable key in the Android Keystore (StrongBox where available).
2. It sends a CSR to IoT Core over a short-lived claim certificate.
3. It receives its own certificate and thing name.

The private key never leaves the device.

## Galaxy Watch (real device)

1. Turn on developer options on the watch and turn on **ADB debugging** and **Debug over Wi-Fi**. Pair with `adb pair`, then `adb connect <watch-ip>:<port>`.
2. Put the Samsung SDK `.aar` in `wear/libs/`, rebuild, and `adb install` the APK.
3. Direct mode:
   - `provision.sh aws --serial <watch>`, or
   - `provision.sh local --host <laptop-LAN-IP> --serial <watch>` against the laptop stack.
4. Companion mode:
   - install `phone-debug.apk` on the paired phone;
   - `provision.sh aws --app phone --serial <phone>`;
   - start the watch with `--es transport phone`.

## Behaviour at the edge

- **Windows:** 30 s / 15 s / 5 s depending on profile (normal / elevated / critical). The cloud changes the profile with `SET_PROFILE` (from the console, or the agent's `SET_PROFILE_ELEVATED` action).
- **Signal quality:** accuracy from each source, plus a motion penalty from the accelerometer, so the cloud can tell artifact from deterioration.
- **Edge rules** run on the watch and work offline: sustained high or low HR, low SpO₂. The watch vibrates and emits an `EDGE_ALERT` event.
- **SOS and fall** events bypass windowing and are queued ahead of telemetry.
- **Offline:** everything goes through the outbox. Items older than 60 s are sent with `buffered=true`, so the cloud scores them as backfill rather than live. The phone's outbox is durable (SQLite); the direct-mode watch keeps up to 5,000 items in memory.
- **Commands** (`SET_PROFILE`, `SPOT_CHECK`, `CHECKIN`, `NOTIFY`) arrive on `rpm/v1/dev/{id}/cmd`. Expired commands (`expires_at`) are ignored.
