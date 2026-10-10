# Watch app (Wear OS) and optional phone companion

| Module | What it is |
|---|---|
| `shared/` | Edge logic with no Android dependency, unit-tested on the JVM: the message contracts, windowing and signal quality, on-device rules, the outbox, the MQTT transport with mutual TLS, and provisioning. |
| `wear/` | The watch app (`com.cloudbox.rpm.wear`). A foreground health service reads the sensors, builds a window every 30 seconds, vibrates on its own alert rules, and publishes over MQTT. The screen shows live heart rate and SpO2, messages and check-in questions from the care team, and a two-tap SOS. |
| `phone/` | Optional companion (`com.cloudbox.rpm.phone`) for a watch with no Wi-Fi or LTE of its own: the watch hands messages to the phone over Bluetooth and the phone publishes them. Not needed for the standard set-up. |

The watch speaks exactly the device contract in [`contracts/`](../../contracts): `rpm.telemetry.v1` on
`rpm/v1/dev/{device}/telemetry`, events on `.../events`, commands on `.../cmd`. The platform treats it like any
other wearable, so a patient can be moved from the simulator to a real watch from the console.

**To connect a watch, follow [docs/watch-setup.md](../../docs/watch-setup.md).** The rest of this page is reference.

## Sensor sources (chosen at start, shown on the watch)

| Source | When | Metrics |
|---|---|---|
| Samsung Health Sensor SDK | Galaxy Watch 4 or later, with the SDK `.aar` in `wear/libs/` | continuous heart rate and HRV, on-demand SpO2, skin temperature |
| Health Services | any other Wear OS 3+ watch, or a Galaxy Watch without the SDK | heart rate |
| Scripted | the emulator, or `--es source scripted` | heart rate, HRV, SpO2, skin temperature, following `normal` / `deteriorate` / `hypoxia` |

The Samsung SDK is licensed separately: download it from Samsung Developers and put the `.aar` in `wear/libs/`.
The build detects it. Without it the app still runs, with heart rate only.

## Build and test

```bash
cd edge/android
./gradlew :shared:test :wear:assembleDebug        # add :phone:assembleDebug for the companion
```

Needs JDK 17+ and Android SDK 35 (`sdk.dir` in `local.properties`). The shared tests check every message the app
emits against the JSON Schemas in `contracts/`, so contract drift fails the build.

## Provisioning

`provision.sh` writes the connection settings and, for TLS, the device certificate into the app's private storage
and restarts the app. The app never falls back to an unauthenticated connection when given an `ssl://` address.

```bash
edge/android/provision.sh plain --host <address>    # no TLS: emulator or a watch on your Wi-Fi, local stack
edge/android/provision.sh local --host <address>    # mutual TLS to the local broker with a development CA
edge/android/provision.sh aws                       # mutual TLS to AWS IoT Core (after `make cloud-device D=<id>`)
```

Options: `--device <id>` (default `gw-demo-01`), `--serial <adb serial>`, `--app phone`.

`local` mirrors AWS IoT Core so the TLS path can be tested without AWS: a client certificate is required, the
client id is forced to the certificate's name, and a device may only use its own topics.

In this project the certificate is created on the laptop and pushed over adb. A production roll-out would use AWS
IoT fleet provisioning, with the private key generated inside the watch's keystore so it never leaves the device.

## Behaviour at the edge

- **Windows:** 30 s, 15 s or 5 s depending on the sampling profile, which the care team or the agents change with
  `SET_PROFILE`.
- **Signal quality:** each reading carries a quality score (sensor accuracy and a motion penalty), so the platform
  can tell an artefact from deterioration.
- **On-device rules** work offline: sustained high or low heart rate, low SpO2. The watch vibrates and sends an
  `EDGE_ALERT`.
- **SOS and falls** skip the window and are sent ahead of telemetry.
- **Offline:** readings are kept (up to 5,000 in memory) and sent on reconnect, marked `buffered` so the platform
  scores them as backfill rather than live.
- **Commands** (`SET_PROFILE`, `SPOT_CHECK`, `CHECKIN`, `NOTIFY`) arrive on the device's `cmd` topic; expired ones
  are ignored. `NOTIFY` is how a clinician's message reaches the screen; a `CHECKIN` answer goes back as an event.

## Debug shortcuts

In debug builds the same actions can be driven from adb: `--es scenario hypoxia`, `--es action sos`,
`--es answer Yes`, for example:

```bash
adb shell am start -n com.cloudbox.rpm.wear/.MainActivity --es scenario deteriorate
```
