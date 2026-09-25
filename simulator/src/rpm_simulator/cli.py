"""rpm-sim: run the device fleet, inject scenarios, inspect the cohort.

  rpm-sim run --mimic-dir ./synthetic_mimic_150patients                    # all 150 patients, local Mosquitto
  rpm-sim scenario --target p-90000012 --type sepsis --duration-s 900      # live injection via control topic
  rpm-sim scenarios | rpm-sim cohort
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
from datetime import UTC, datetime

from rpm_common.data import fetch_dir
from rpm_common.logging import setup_logging
from rpm_mimic import MimicDataset, build_cohort, icu_series, pick_demo_patient

from .engine import CMD_FILTER, CONTROL_TOPIC, Simulator
from .hl7_monitor import Hl7MonitorLink
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
    p.add_argument("--mimic-dir", default=_env("RPM_MIMIC_DIR", "./synthetic_mimic_150patients"),
                   help="local directory or s3://bucket/prefix")
    p.add_argument("--icu-count", type=int, default=int(_env("RPM_ICU_COUNT", "25")))
    p.add_argument("--seed", default=_env("RPM_COHORT_SEED", "rpm-v1"))


def _mqtt_config(a: argparse.Namespace, shards: int = 1) -> MqttConfig:
    return MqttConfig(host=a.mqtt_host, port=a.mqtt_port, client_prefix=a.client_prefix, shards=shards,
                      tls_ca=a.tls_ca, tls_cert=a.tls_cert, tls_key=a.tls_key)


def cmd_run(a: argparse.Namespace) -> None:
    ds = MimicDataset(fetch_dir(a.mimic_dir))
    members = build_cohort(ds, datetime.now(UTC).replace(tzinfo=None), icu_count=a.icu_count, seed=a.seed)
    excluded = set()
    for x in a.exclude or []:  # "auto" resolves exactly like the loader's demo-patient choice
        m = pick_demo_patient(members, x)
        excluded.add(m.patient_id if m else x)
    members = [m for m in members if m.patient_id not in excluded and m.subject_id not in excluded]
    if a.only_setting:
        members = [m for m in members if m.care_setting == a.only_setting]
    if a.limit:
        members = members[: a.limit]
    series = {m.subject_id: icu_series(ds, m.index_stay_id) for m in members
              if m.care_setting == "icu" and m.index_stay_id}

    sim_ref: dict[str, Simulator] = {}
    pub = FleetPublisher(_mqtt_config(a, a.shards), on_message=lambda t, p: sim_ref["sim"].on_message(t, p))
    monitors = None
    if a.icu_hl7:  # ICU beds leave as HL7 v2 to the bedside gateway instead of publishing MQTT themselves
        host, _, port = a.icu_hl7.rpartition(":")
        monitors = Hl7MonitorLink(host, int(port))
    icu = {m.device_id for m in members if m.care_setting == "icu"}

    def publish(key: str, topic: str, payload: str) -> None:
        if monitors is not None and key in icu:
            if topic.endswith("/telemetry"):
                monitors.send(key, payload)
            return  # an HL7 results feed carries observations only
        pub.publish(key, topic, payload)

    sim = Simulator(members, series, publish, window_s=a.window_s, speed=a.speed, seed=a.seed,
                    tz_offset_h=a.tz_offset_h, auto_scenarios_per_hour=a.auto_scenarios_per_hour,
                    icu_period_s=a.icu_period_s if monitors else None)
    sim_ref["sim"] = sim
    pub.subscribe(CONTROL_TOPIC)
    pub.subscribe(CMD_FILTER)
    pub.connect()
    homes = sum(1 for m in members if m.care_setting == "home")
    log.info("simulator started", extra={"devices": len(members), "home": homes, "icu": len(members) - homes,
                                         "window_s": a.window_s, "speed": a.speed, "shards": a.shards,
                                         "icu_via": f"hl7://{a.icu_hl7}" if monitors else "mqtt"})
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        sim.run(stop, duration_s=a.duration_s)
    finally:
        pub.close()
        if monitors is not None:
            monitors.close()


def _send_control(a: argparse.Namespace, payload: dict) -> None:
    # A unique client id: reusing the fleet's id would make the broker drop the running simulator's session
    # (MQTT client-id takeover) exactly when the control message is published.
    a.client_prefix = f"sim-ctl-{uuid.uuid4().hex[:8]}"
    pub = FleetPublisher(_mqtt_config(a))
    pub.connect()
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


def cmd_scenarios(_a: argparse.Namespace) -> None:
    for s in SCENARIOS.values():
        print(f"{s.name:20s} {s.default_duration_s:>5.0f}s  {s.description}")


def cmd_cohort(a: argparse.Namespace) -> None:
    ds = MimicDataset(fetch_dir(a.mimic_dir))
    members = build_cohort(ds, datetime.now(UTC).replace(tzinfo=None), icu_count=a.icu_count, seed=a.seed)
    print(f"{'patient':12s} {'name':20s} {'sex':3s} {'age':>3s} {'condition':9s} {'setting':7s} device")
    for m in members:
        print(f"{m.patient_id:12s} {m.display_name:20s} {m.sex:3s} {m.age:>3d} {m.condition:9s} "
              f"{m.care_setting:7s} {m.device_id}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="rpm-sim", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the virtual device fleet")
    _cohort_args(run)
    _mqtt_args(run)
    run.add_argument("--shards", type=int, default=int(_env("RPM_SIM_SHARDS", "1")))
    run.add_argument("--window-s", type=int, default=int(_env("RPM_SIM_WINDOW_S", "30")))
    run.add_argument("--speed", type=float, default=float(_env("RPM_SIM_SPEED", "60")),
                     help="ICU replay speed-up (sim seconds per real second)")
    run.add_argument("--limit", type=int, default=int(_env("RPM_SIM_LIMIT", "0")))
    run.add_argument("--only-setting", choices=["home", "icu"], default=_env("RPM_SIM_ONLY_SETTING"))
    run.add_argument("--icu-hl7", default=_env("RPM_SIM_ICU_HL7"), metavar="HOST:PORT",
                     help="send ICU beds as HL7 v2 ORU^R01 over MLLP to the ICU gateway instead of MQTT")
    run.add_argument("--icu-period-s", type=int, default=int(_env("RPM_SIM_ICU_PERIOD_S", "5")),
                     help="bedside monitor reporting cadence when --icu-hl7 is set")
    run.add_argument("--exclude", nargs="*", default=(_env("RPM_SIM_EXCLUDE", "") or "").split() or None,
                     help="patient ids streamed by real devices (e.g. the Galaxy Watch demo patient)")
    run.add_argument("--auto-scenarios-per-hour", type=float, default=float(_env("RPM_SIM_AUTO_SCENARIOS", "0")))
    run.add_argument("--tz-offset-h", type=float, default=float(_env("RPM_SIM_TZ_OFFSET_H", "4")))
    run.add_argument("--duration-s", type=float, default=0, help="stop after N seconds (0 = forever)")
    run.set_defaults(fn=cmd_run)

    sc = sub.add_parser("scenario", help="inject a scenario into a running simulator")
    _mqtt_args(sc)
    sc.add_argument("--target", required=True, help="patient id (p-...), subject id or device id")
    sc.add_argument("--type", required=True)
    sc.add_argument("--duration-s", type=float)
    sc.add_argument("--ramp-s", type=float, help="override ramp-in time (e.g. 90 for a live demo)")
    sc.set_defaults(fn=cmd_scenario)

    cl = sub.add_parser("clear", help="clear a patient's active scenario")
    _mqtt_args(cl)
    cl.add_argument("--target", required=True)
    cl.set_defaults(fn=cmd_clear)

    sub.add_parser("scenarios", help="list scenarios").set_defaults(fn=cmd_scenarios)
    co = sub.add_parser("cohort", help="print the deterministic demo cohort")
    _cohort_args(co)
    co.set_defaults(fn=cmd_cohort)

    a = ap.parse_args(argv)
    setup_logging("simulator", _env("RPM_LOG_LEVEL", "INFO"))
    a.fn(a)


if __name__ == "__main__":
    main()
