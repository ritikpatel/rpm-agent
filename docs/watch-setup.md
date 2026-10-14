# Connecting a Galaxy Watch

This takes a home (discharged) patient off the simulator and onto a real watch. Do it against the local stack
first, then against AWS. The watch app is in `edge/android`; it needs no code changes.

## What you need

- A Galaxy Watch 4 or later (any Wear OS 3+ watch works, with heart rate only).
- A Mac with Android Studio or the Android SDK, and JDK 17 or later.
- The watch and the Mac on the same Wi-Fi network.
- Optional, for SpO2 and skin temperature on a Galaxy Watch: the Samsung Health Sensor SDK `.aar` from Samsung
  Developers, copied to `edge/android/wear/libs/`.

## 1. Build the app

```bash
cd edge/android
./gradlew :shared:test :wear:assembleDebug
```

The APK is `wear/build/outputs/apk/debug/wear-debug.apk`.

## 2. Connect the watch to adb over Wi-Fi

On the watch: **Settings → About watch → Software information**, tap **Software version** seven times to enable
developer options. Then **Settings → Developer options**: turn on **ADB debugging** and **Wireless debugging**, and
choose **Pair new device**. The watch shows an address, a port and a code.

```bash
adb pair <watch-ip>:<pairing-port>        # type the code
adb connect <watch-ip>:<port>             # the port shown on the Wireless debugging screen
adb devices                               # note the serial, e.g. 192.168.1.42:39447
```

## 3. Install and grant the sensor permission

```bash
adb -s <serial> install -r wear/build/outputs/apk/debug/wear-debug.apk
adb -s <serial> shell pm grant com.cloudbox.rpm.wear android.permission.BODY_SENSORS
```

## 4a. Point it at the local stack

Find the Mac's Wi-Fi address (`ipconfig getifaddr en0`), then from the repo root:

```bash
make up && make seed && make sim
edge/android/provision.sh local --host <mac-ip> --device gw-demo-01 --serial <serial>
```

`local` uses mutual TLS with a development certificate, the same way AWS does. `plain` in place of `local` skips
TLS if you only want a quick check.

## 4b. Or point it at AWS

After the platform is deployed (see [runbook.md](runbook.md)):

```bash
make cloud-device D=gw-demo-01            # creates the device and its certificate in AWS IoT Core
edge/android/provision.sh aws --device gw-demo-01 --serial <serial>
```

The certificate is written to the git-ignored `.secrets/devices/gw-demo-01/` and pushed into the app's private
storage.

## 5. Give the watch to a patient

In the console, open a **home** patient, find the **Watch** card, type `gw-demo-01` and click **Assign watch**.
The simulator stops sending for that patient within 15 seconds, so the watch is the only source.

A watch can only be assigned to a patient at home. Admitted patients are on ICU monitors.

## 6. Check it works

| Do this | Expect |
|---|---|
| Wear the watch for a minute | The patient's row flashes in the worklist; the chart shows your heart rate; the device reads `gw-demo-01` |
| Console: **Messages with the patient**, send a message | It appears on the watch |
| Console: **Ask: short of breath?** | The question appears on the watch; tap Yes or No; the answer shows in the message list |
| Console: **Request SpO₂ reading** (needs the Samsung SDK) | The watch measures and the value appears in the chart |
| Watch: press SOS twice | A critical alert opens in the console within a second and an e-mail goes out |
| Turn the watch's Wi-Fi off for two minutes, then on | The missing readings arrive marked as backfilled; no false alert |

To hand the patient back to the simulator, assign the simulator's device id: `sim-` plus the patient id, for
example `sim-SYN-000160`.

A watch taken off or switched off raises no alert: silent devices are recorded but not alerted on in this
demonstration (`RPM_SILENT_DEVICE_ALERTS`, see the runbook).

## No watch to hand: the emulator

```bash
~/Library/Android/sdk/emulator/emulator -avd <wear-os-avd> &
adb install -r edge/android/wear/build/outputs/apk/debug/wear-debug.apk
adb shell pm grant com.cloudbox.rpm.wear android.permission.BODY_SENSORS
adb shell am start -n com.cloudbox.rpm.wear/.MainActivity \
  --es transport direct --es device_id gw-demo-01 --es source scripted
```

The emulator reaches the Mac's broker at `10.0.2.2`. The scripted source plays `normal`, `deteriorate` or
`hypoxia` (`--es scenario hypoxia`). Assign `gw-demo-01` to a home patient as in step 5.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `adb connect` fails | The watch sleeps its Wi-Fi: wake it and reopen Wireless debugging; the port changes each time |
| App shows "not connected" | Wrong address or the Mac's firewall blocks port 8883 (1883 for `plain`); re-run `provision.sh` with the right `--host` |
| Not connected, against AWS only | The Wi-Fi network blocks outgoing port 8883 (common on campus and office networks): put the watch on a phone hotspot |
| Connected, but nothing in the console | The device is not assigned to a patient (step 5), or it is assigned to a patient in the ICU |
| TLS error against AWS | The certificate was not created for this device id: run `make cloud-device D=<id>` and provision again |
| Heart rate only on a Galaxy Watch | The Samsung SDK `.aar` is missing from `wear/libs/`; add it and rebuild |
| Readings stop after a few minutes | Battery optimisation: on the watch allow the app to run in the background |
