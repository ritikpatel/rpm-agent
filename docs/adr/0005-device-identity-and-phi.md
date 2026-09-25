# ADR 0005 — Device identity, topic design and PHI minimisation

**Status:** accepted · 2026-09-25

- One AWS IoT thing + X.509 certificate per real device. The policy uses `${iot:Connection.Thing.ThingName}`, so a
  device can only connect as itself and use its own `rpm/v1/dev/{thing}/*` topics and shadow.
- **No patient identifiers on the wire.** Topics and payloads carry only the thing name. The device → patient
  mapping (`device_assignments`, FHIR `Device.patient`) exists only server-side.
- The cloud trusts the **topic**, not the payload: the IoT rule adds `topic(4)` as `thing`, and the stream engine
  rejects messages whose `device_id` differs (counted as `device_mismatch`).
- The simulator's fleet certificate is scoped to `sim-*` topics and cannot publish to or receive a real device's
  topics. Control/CLI clients use unique `sim-ctl-*` client ids (a duplicate client id makes the broker drop the
  running session — found and fixed in week 1).
- Demo shortcut: AWS-generated keys for `gw-demo-01` are written to git-ignored `.secrets/`. Week 3 moves to fleet
  provisioning by claim, with the private key generated on the device (Android Keystore).
