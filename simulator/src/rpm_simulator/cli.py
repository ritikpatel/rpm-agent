"""rpm-sim: simulated wearables and ICU monitors.

  rpm-sim run                                           # ICU monitors for admitted patients, wearables at home;
                                                        # new readings carry on from each patient's last recorded one
  rpm-sim scenario --target SYN-000143 --type sepsis    # make a patient deteriorate (the running fleet reacts)
  rpm-sim discharge --target RPM-10001725               # the ICU monitor stops, the patient's wearable starts
  rpm-sim send --target SYN-000143 --hr 134 --spo2 86   # one reading, sent right now
  rpm-sim send --target SYN-000143 --event SOS          # the patient presses SOS
  rpm-sim scenarios | rpm-sim cohort

The same commands work against the local broker (default) and AWS IoT Core: pass the endpoint and the simulator
certificate with --mqtt-host/--mqtt-port 8883/--tls-*, or set the RPM_MQTT_* variables (`make sim-cloud` does this).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import uuid
from datetime import UTC, datetime, timedelta

import httpx

from rpm_core.cohort import TrainingData, build_cohort, sim_device_id
from rpm_core.contracts import DeviceEvent, DeviceEventTs, Metrics, Telemetry, TelemetryTs, telemetry_topic, utcnow
from rpm_core.data import fetch_dir
from rpm_core.logging import setup_logging

from .engine import CMD_FILTER, CONTROL_TOPIC, Simulator
from .publisher import FleetPublisher, MqttConfig
from .scenarios import SCENARIOS

log = logging.getLogger("rpm_simulator")


def _env(name: str, default=None):
    return os.environ.get(name, default)


def _mqtt_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--mqtt-host", default=_env("RPM_MQTT_HOST", "localhost"))
    p.add_argument("--mqtt-port", type=int, default=int(_env("RPM_MQTT_PORT", "1883")))
    p.add_argument("--tls-ca", default=_env("RPM_MQTT_TLS_CA"))
    p.add_argument("--tls-cert", default=_env("RPM_MQTT_TLS_CERT"))
    p.add_argument("--tls-key", default=_env("RPM_MQTT_TLS_KEY"))
    p.add_argument("--client-prefix", default=_env("RPM_MQTT_CLIENT_PREFIX", "sim-fleet"))


def _cohort_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data-dir", default=_env("RPM_DATA_DIR", "./Training Data"),
                   help="the team's training data (local directory or s3://bucket/prefix)")
    p.add_argument("--icu", type=int, default=int(_env("RPM_COHORT_ICU", "6")),
                   help="patients shown as admitted to the ICU")
    p.add_argument("--home", type=int, default=int(_env("RPM_COHORT_HOME", "24")),
                   help="discharged patients on wearables (0 = everyone else in the data)")
    p.add_argument("--seed", default=_env("RPM_COHORT_SEED", "rpm-v1"))


def _cohort(a: argparse.Namespace):
    data = TrainingData(fetch_dir(a.data_dir))
    return build_cohort(data, datetime.now(UTC).replace(tzinfo=None), a.icu, a.home, a.seed)


def _mqtt_config(a: argparse.Namespace, shards: int = 1) -> MqttConfig:
    return MqttConfig(host=a.mqtt_host, port=a.mqtt_port, client_prefix=a.client_prefix, shards=shards,
                      tls_ca=a.tls_ca, tls_cert=a.tls_cert, tls_key=a.tls_key)


def _sync(sim: Simulator, http: httpx.Client) -> bool:
    try:
        r = http.get("/internal/cohort")
        r.raise_for_status()
        sim.sync(r.json())
        return True
    except Exception as exc:  # noqa: BLE001 - the fleet keeps running on its last known state
        log.debug("registry sync failed", extra={"error": str(exc)[:200]})
        return False


def _sync_loop(sim: Simulator, http: httpx.Client, stop: threading.Event, interval_s: float = 15) -> None:
    """Follow the platform's registry so a restart, a discharge or a newly assigned real watch is picked up."""
    while not stop.wait(interval_s):
        _sync(sim, http)


def cmd_run(a: argparse.Namespace) -> None:
    members = _cohort(a)
    sim_ref: dict[str, Simulator] = {}
    pub = FleetPublisher(_mqtt_config(a, a.shards), on_message=lambda t, p: sim_ref["sim"].on_message(t, p))
    sim = Simulator(members, pub.publish, window_s=a.window_s, seed=a.seed,
                    tz_offset_h=a.tz_offset_h, auto_scenarios_per_hour=a.auto_scenarios_per_hour,
                    icu_window_s=a.icu_window_s, recovery_half_life_h=a.recovery_half_life_h)
    sim_ref["sim"] = sim
    pub.subscribe(CONTROL_TOPIC)
    pub.subscribe(CMD_FILTER)
    pub.connect()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if a.care_api:
        http = httpx.Client(base_url=a.care_api.rstrip("/"), timeout=5,
                            headers={"X-Service-Token": a.service_token, "X-Service-Name": "simulator"})
        for _ in range(10):   # before the first reading: carry on from what each device last reported
            if _sync(sim, http) or stop.wait(3):
                break
        threading.Thread(target=_sync_loop, name="registry-sync", daemon=True, args=(sim, http, stop)).start()
    homes = sum(1 for m in members if m.care_setting == "home")
    log.info("simulator started", extra={"patients": len(members), "home_wearables": homes,
                                         "icu_monitors": len(members) - homes, "window_s": a.window_s,
                                         "icu_window_s": a.icu_window_s, "registry_sync": bool(a.care_api)})
    try:
        sim.run(stop, duration_s=a.duration_s)
    finally:
        pub.close()


def _publisher(a: argparse.Namespace) -> FleetPublisher:
    # A unique client id: reusing the fleet's id would make the broker drop the running simulator's session
    # (MQTT client-id takeover) exactly when this message is published.
    a.client_prefix = f"sim-ctl-{uuid.uuid4().hex[:8]}"
    pub = FleetPublisher(_mqtt_config(a))
    pub.connect()
    return pub


def _send_control(a: argparse.Namespace, payload: dict) -> None:
    pub = _publisher(a)
    pub.publish("control", CONTROL_TOPIC, json.dumps(payload))
    pub.close()
    print(f"sent: {payload}")


def cmd_scenario(a: argparse.Namespace) -> None:
    if a.type not in SCENARIOS:
        sys.exit(f"unknown scenario {a.type!r}; choose from {', '.join(SCENARIOS)}")
    _send_control(a, {"action": "scenario", "target": a.target, "scenario": a.type, "duration_s": a.duration_s,
                      "ramp_s": a.ramp_s})


def cmd_clear(a: argparse.Namespace) -> None:
    _send_control(a, {"action": "clear", "target": a.target})


def cmd_setting(a: argparse.Namespace) -> None:
    """Tells the simulator only. The platform learns of a discharge from the hospital record (the console's
    Discharge button, or a FHIR Encounter update), which also sends this message."""
    _send_control(a, {"action": a.command, "target": a.target})


def cmd_send(a: argparse.Namespace) -> None:
    """One message, sent as the patient's own device would send it."""
    device = a.device or sim_device_id(a.target, a.setting)
    source = "icu_monitor" if a.setting == "icu" else "simulator"
    now = utcnow()
    seq = int(now.timestamp())  # larger than anything a fleet has counted to, so it is never taken for a replay
    if a.event:
        data = {"trigger": "simulated"} if a.event in ("SOS", "FALL") else \
            {"question_id": a.question_id, "answer": a.answer}
        msg = DeviceEvent(device_id=device, seq=seq, source=source, type=a.event,
                          ts=DeviceEventTs(occurred=now, edge_sent=now), data=data)
        kind = "events"
    else:
        metrics: dict = {}
        if a.hr is not None:
            metrics["heart_rate"] = {"mean": a.hr, "min": a.hr - 2, "max": a.hr + 2, "n": 30, "quality": 0.97}
        for name, value in (("spo2", a.spo2), ("resp_rate", a.rr), ("sbp", a.sbp), ("dbp", a.dbp), ("temp", a.temp),
                            ("gcs", a.gcs)):
            if value is not None:
                metrics[name] = {"value": value, **({"quality": 0.95} if name == "spo2" else {})}
        if not metrics:
            sys.exit("nothing to send: give at least one of --hr --spo2 --rr --sbp --dbp --temp --gcs, or --event")
        msg = Telemetry(device_id=device, seq=seq, source=source, context=a.setting,
                        ts=TelemetryTs(window_start=now - timedelta(seconds=30), window_end=now, edge_sent=now),
                        metrics=Metrics.model_validate(metrics))
        kind = "telemetry"
    pub = _publisher(a)
    for _ in range(max(1, a.repeat)):
        pub.publish(device, telemetry_topic(device, kind), msg.to_json())
    pub.close()
    print(f"sent {kind} from {device}: {msg.to_json()}")


def cmd_scenarios(_a: argparse.Namespace) -> None:
    for s in SCENARIOS.values():
        print(f"{s.name:20s} {s.default_duration_s:>5.0f}s  {s.description}")


def cmd_cohort(a: argparse.Namespace) -> None:
    members = _cohort(a)
    print(f"{'patient':13s} {'name':18s} {'sex':3s} {'age':>3s} {'setting':7s} {'source':10s} diagnosis")
    for m in members:
        print(f"{m.patient_id:13s} {m.display_name:18s} {m.sex:3s} {m.age:>3d} {m.care_setting:7s} "
              f"{'MIMIC' if m.origin == 'mimic_real' else 'synthetic':10s} {m.diagnosis or '-'}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="rpm-sim", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the simulated devices")
    _cohort_args(run)
    _mqtt_args(run)
    run.add_argument("--shards", type=int, default=int(_env("RPM_SIM_SHARDS", "1")))
    run.add_argument("--window-s", type=int, default=int(_env("RPM_SIM_WINDOW_S", "30")),
                     help="wearable reporting window")
    run.add_argument("--icu-window-s", type=int, default=int(_env("RPM_SIM_ICU_WINDOW_S", "10")),
                     help="ICU monitor reporting cadence")
    run.add_argument("--care-api", default=_env("RPM_CARE_API_URL"),
                     help="follow the platform's registry (admissions, discharges, real watches)")
    run.add_argument("--service-token", default=_env("RPM_SERVICE_TOKEN", "dev-service-token"))
    run.add_argument("--auto-scenarios-per-hour", type=float, default=float(_env("RPM_SIM_AUTO_SCENARIOS", "0.25")),
                     help="how often a settled patient deteriorates on their own, across everyone (0 = never)")
    run.add_argument("--recovery-half-life-h", type=float,
                     default=float(_env("RPM_SIM_RECOVERY_HALF_LIFE_H", "2")),
                     help="patients whose record ended with them unwell: hours for the gap to well to halve "
                          "(0 = they stay as the record left them)")
    run.add_argument("--tz-offset-h", type=float, default=float(_env("RPM_SIM_TZ_OFFSET_H", "5.5")))
    run.add_argument("--duration-s", type=float, default=0, help="stop after N seconds (0 = forever)")
    run.set_defaults(fn=cmd_run)

    sc = sub.add_parser("scenario", help="make a patient deteriorate in the running fleet")
    _mqtt_args(sc)
    sc.add_argument("--target", required=True, help="patient id (RPM-... or SYN-...), subject id or device id")
    sc.add_argument("--type", required=True)
    sc.add_argument("--duration-s", type=float)
    sc.add_argument("--ramp-s", type=float, help="override ramp-in time (for example 60 for a live demo)")
    sc.set_defaults(fn=cmd_scenario)

    cl = sub.add_parser("clear", help="end a patient's scenario")
    _mqtt_args(cl)
    cl.add_argument("--target", required=True)
    cl.set_defaults(fn=cmd_clear)

    for name, text in (("discharge", "switch a patient's simulated device from ICU monitor to wearable"),
                       ("admit", "switch a patient's simulated device from wearable to ICU monitor")):
        sp = sub.add_parser(name, help=text)
        _mqtt_args(sp)
        sp.add_argument("--target", required=True)
        sp.set_defaults(fn=cmd_setting)

    se = sub.add_parser("send", help="send one reading or device event right now")
    _mqtt_args(se)
    se.add_argument("--target", help="patient id; the device id is derived from it and --setting")
    se.add_argument("--device", help="device id, instead of --target")
    se.add_argument("--setting", choices=["home", "icu"], default="home")
    for flag in ("hr", "spo2", "rr", "sbp", "dbp", "temp", "gcs"):
        se.add_argument(f"--{flag}", type=float)
    se.add_argument("--event", choices=["SOS", "FALL", "CHECKIN_REPLY"])
    se.add_argument("--answer", default="Yes", help="for --event CHECKIN_REPLY")
    se.add_argument("--question-id", default="breathless-manual", help="for --event CHECKIN_REPLY")
    se.add_argument("--repeat", type=int, default=1)
    se.set_defaults(fn=cmd_send)

    sub.add_parser("scenarios", help="list scenarios").set_defaults(fn=cmd_scenarios)
    co = sub.add_parser("cohort", help="print the monitored patients")
    _cohort_args(co)
    co.set_defaults(fn=cmd_cohort)

    a = ap.parse_args(argv)
    if a.command == "send" and not (a.target or a.device):
        ap.error("send needs --target or --device")
    setup_logging("simulator", _env("RPM_LOG_LEVEL", "INFO"))
    a.fn(a)


if __name__ == "__main__":
    main()
