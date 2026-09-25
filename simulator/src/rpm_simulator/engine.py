"""Fleet simulator: one virtual device per cohort patient, publishing the exact contracts real devices use,
reacting to cloud commands (SET_PROFILE, SPOT_CHECK, CHECKIN, NOTIFY) and to scenario control messages."""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from rpm_contracts import (
    PROFILE_WINDOW_S,
    Command,
    DeviceEvent,
    DeviceEventTs,
    DeviceInfo,
    EdgeInfo,
    Metrics,
    Telemetry,
    TelemetryTs,
    telemetry_topic,
    utcnow,
)
from rpm_mimic import CohortMember

from .physiology import HomeWearable, IcuReplay
from .scenarios import SCENARIOS, ActiveScenario

log = logging.getLogger(__name__)

CONTROL_TOPIC = "rpm/v1/sim/control"
CMD_FILTER = "rpm/v1/dev/+/cmd"
CLINICAL_SCENARIOS = ["sepsis", "chf_decompensation", "copd_exacerbation", "hypoxia", "af_rvr", "bradycardia"]

Publish = Callable[[str, str, str], None]  # (key, topic, payload)


@dataclass
class SimDevice:
    member: CohortMember
    gen: HomeWearable | IcuReplay
    next_due: float
    seq: int = 0
    profile: str = "normal"
    window_s: int = 30
    scenario: ActiveScenario | None = None
    outbox: list[Telemetry] = field(default_factory=list)
    battery: float = 100.0
    pending: list[tuple[float, str, dict]] = field(default_factory=list)  # (due, event_type, data)
    force_spo2: bool = False

    @property
    def device_id(self) -> str:
        return self.member.device_id

    @property
    def context(self) -> str:
        return self.member.care_setting

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq


class Simulator:
    def __init__(self, members: list[CohortMember], series: dict[str, dict], publish: Publish, *,
                 window_s: int = 30, speed: float = 60.0, seed: str = "rpm-v1", tz_offset_h: float = 4.0,
                 auto_scenarios_per_hour: float = 0.0, manual_every_s: float = 1200):
        self.publish = publish
        self.speed = speed
        self.tz_offset_h = tz_offset_h
        self.auto_rate = auto_scenarios_per_hour
        self.default_manual_every_s = manual_every_s
        self.rng = random.Random(seed)
        self.t0 = time.monotonic()
        self.lock = threading.Lock()
        self.devices: dict[str, SimDevice] = {}
        stagger = random.Random(f"{seed}:stagger")
        for i, m in enumerate(members):
            rng = random.Random(f"{seed}:{m.subject_id}")
            if m.care_setting == "icu" and series.get(m.subject_id):
                # a bedside monitor always shows every channel: fill uncharted ones from the patient baseline
                full = {k: [(0.0, v)] for k, v in m.baseline.items()} | {"gcs": [(0.0, 15.0)]}
                full.update({k: v for k, v in series[m.subject_id].items() if v})
                gen: HomeWearable | IcuReplay = IcuReplay(full, rng, m.replay_start_h, speed)
            else:
                gen = HomeWearable(m.baseline, rng, manual_every_s=manual_every_s)
            offset = (i * 0.37 + stagger.random()) % window_s  # spread publishes across the window
            self.devices[m.device_id] = SimDevice(m, gen, next_due=self.t0 + offset, window_s=window_s)
        self.by_alias: dict[str, str] = {}
        for d in self.devices.values():
            for alias in (d.device_id, d.member.patient_id, d.member.subject_id):
                self.by_alias[alias] = d.device_id

    # ------------------------------------------------------------------ control plane
    def resolve(self, target: str) -> SimDevice | None:
        dev_id = self.by_alias.get(target)
        return self.devices.get(dev_id) if dev_id else None

    def start_scenario(self, target: str, name: str, duration_s: float | None = None,
                       ramp_s: float | None = None) -> bool:
        spec = SCENARIOS.get(name)
        dev = self.resolve(target)
        if not spec or not dev:
            log.warning("scenario rejected", extra={"target": target, "scenario": name})
            return False
        with self.lock:
            dev.scenario = ActiveScenario(spec, time.monotonic(), duration_s or spec.default_duration_s,
                                          ramp_override_s=ramp_s)
            if isinstance(dev.gen, HomeWearable) and spec.deltas:
                dev.gen.manual_every_s = 300  # patient is prompted for extra home readings
        log.info("scenario started", extra={"device_id": dev.device_id, "patient_id": dev.member.patient_id,
                                            "scenario": name, "duration_s": dev.scenario.duration_s})
        if spec.emit_event:  # a button press / fall is published immediately, not on the next telemetry tick
            dev.scenario.event_emitted = True
            self._event(dev, spec.emit_event, {"trigger": "simulated", "scenario": spec.name})
        return True

    def clear_scenario(self, target: str) -> None:
        dev = self.resolve(target)
        if dev:
            with self.lock:
                dev.scenario = None
                if isinstance(dev.gen, HomeWearable):
                    dev.gen.manual_every_s = self.default_manual_every_s

    def on_message(self, topic: str, payload: bytes) -> None:
        if topic == CONTROL_TOPIC:
            msg = json.loads(payload)
            if msg.get("action") == "scenario":
                self.start_scenario(str(msg["target"]), msg["scenario"], msg.get("duration_s"), msg.get("ramp_s"))
            elif msg.get("action") == "clear":
                self.clear_scenario(str(msg["target"]))
            return
        parts = topic.split("/")
        if len(parts) == 5 and parts[4] == "cmd" and parts[3] in self.devices:
            self.handle_command(self.devices[parts[3]], Command.model_validate_json(payload))

    def handle_command(self, dev: SimDevice, cmd: Command) -> None:
        if cmd.expired():
            log.info("expired command ignored", extra={"device_id": dev.device_id, "cmd": cmd.cmd})
            return
        now = time.monotonic()
        args = cmd.args or {}
        log.info("command received", extra={"device_id": dev.device_id, "cmd": cmd.cmd, "cmd_args": args})
        with self.lock:
            if cmd.cmd == "SET_PROFILE" and args.get("profile") in PROFILE_WINDOW_S:
                dev.profile = args["profile"]
                dev.window_s = PROFILE_WINDOW_S[dev.profile]
                dev.next_due = min(dev.next_due, now + dev.window_s)
            elif cmd.cmd == "SPOT_CHECK":
                dev.pending.append((now + 20, "SPOT_CHECK_RESULT", {"metric": args.get("metric", "spo2"),
                                                                    "cmd_id": str(cmd.cmd_id)}))
            elif cmd.cmd == "CHECKIN":
                options = args.get("options") or ["Yes", "No"]
                unwell = dev.scenario is not None and dev.scenario.in_hold(now) and bool(dev.scenario.spec.deltas)
                answer = options[0] if unwell else options[-1]
                dev.pending.append((now + self.rng.uniform(8, 25), "CHECKIN_REPLY",
                                    {"question_id": args.get("question_id"), "answer": answer,
                                     "cmd_id": str(cmd.cmd_id)}))

    # ------------------------------------------------------------------ data plane
    def _deltas(self, dev: SimDevice, now: float) -> tuple[dict[str, float], bool, bool, bool]:
        sc = dev.scenario
        if sc is None:
            return {}, False, False, False
        if sc.finished(now):
            self.clear_scenario(dev.device_id)
            log.info("scenario finished", extra={"device_id": dev.device_id, "scenario": sc.spec.name})
            return {}, False, False, False
        k = sc.intensity(now)
        hold = sc.in_hold(now)
        return ({m: v * k for m, v in sc.spec.deltas.items()}, sc.spec.motion_artifact and hold,
                sc.spec.arrhythmia and k > 0.5, sc.spec.offline and hold)

    def _event(self, dev: SimDevice, etype: str, data: dict) -> None:
        now = utcnow()
        ev = DeviceEvent(device_id=dev.device_id, seq=dev.next_seq(), source="simulator", type=etype,
                         ts=DeviceEventTs(occurred=now, edge_sent=now), data=data)
        self.publish(dev.device_id, telemetry_topic(dev.device_id, "events"), ev.to_json())

    @staticmethod
    def _edge_flags(metrics: dict) -> list[str]:
        """Mimic the watch's on-device rules (the real app adds sustain logic and haptics)."""
        flags = []
        hr = metrics.get("heart_rate")
        if hr and hr.get("quality", 1) >= 0.6 and hr["mean"] > 130:
            flags.append("HR_HIGH")
        spo2 = metrics.get("spo2")
        if spo2 and spo2["value"] < 90:
            flags.append("SPO2_LOW")
        return flags

    def _produce(self, dev: SimDevice, now: float) -> None:
        deltas, motion, arrhythmia, offline = self._deltas(dev, now)
        sc = dev.scenario
        if sc and sc.spec.emit_event and not sc.event_emitted:
            sc.event_emitted = True
            self._event(dev, sc.spec.emit_event, {"trigger": "simulated", "scenario": sc.spec.name})

        wall = utcnow()
        if isinstance(dev.gen, IcuReplay):
            w = dev.gen.sample(now - self.t0, deltas, arrhythmia)
        else:
            hour = (wall.hour + wall.minute / 60 + self.tz_offset_h) % 24
            w = dev.gen.sample(now, hour, dev.window_s, deltas, motion, arrhythmia, force_spo2=dev.force_spo2)
            dev.force_spo2 = False
        dev.battery = max(5.0, dev.battery - 0.01 * dev.window_s / 30)
        msg = Telemetry(
            device_id=dev.device_id, seq=dev.next_seq(), source="simulator", context=dev.context,
            ts=TelemetryTs(window_start=wall - timedelta(seconds=dev.window_s), window_end=wall, edge_sent=wall),
            metrics=Metrics.model_validate(w.metrics),
            edge=EdgeInfo(flags=self._edge_flags(w.metrics), profile=dev.profile, buffered=offline),
            device=DeviceInfo(battery_pct=round(dev.battery, 1), on_wrist=True, fw="sim-0.1.0"),
        )
        if offline:
            dev.outbox.append(msg)  # store-and-forward, exactly like the phone gateway
            return
        if dev.outbox:
            log.info("backfilling buffered telemetry", extra={"device_id": dev.device_id, "count": len(dev.outbox)})
            for old in dev.outbox:
                old.ts.edge_sent = utcnow()
                self.publish(dev.device_id, telemetry_topic(dev.device_id, "telemetry"), old.to_json())
            dev.outbox.clear()
        self.publish(dev.device_id, telemetry_topic(dev.device_id, "telemetry"), msg.to_json())

    def _pending(self, dev: SimDevice, now: float) -> None:
        due = [p for p in dev.pending if p[0] <= now]
        if not due:
            return
        dev.pending = [p for p in dev.pending if p[0] > now]
        for _, etype, data in due:
            if etype == "SPOT_CHECK_RESULT":
                deltas, *_ = self._deltas(dev, now)
                base = dev.gen.b["spo2"] if isinstance(dev.gen, HomeWearable) else 95.0
                data = {**data, "value": round(min(100, base + deltas.get("spo2", 0) + self.rng.gauss(0, 0.5)), 1),
                        "quality": 0.92}
            self._event(dev, etype, data)

    def _maybe_auto_scenario(self, dt: float) -> None:
        if self.auto_rate <= 0 or self.rng.random() > self.auto_rate * dt / 3600:
            return
        idle = [d for d in self.devices.values() if d.scenario is None]
        if idle:
            dev = self.rng.choice(idle)
            self.start_scenario(dev.device_id, self.rng.choice(CLINICAL_SCENARIOS))

    def tick(self, dt: float = 1.0) -> int:
        now = time.monotonic()
        sent = 0
        with self.lock:
            devices = list(self.devices.values())
        for dev in devices:
            self._pending(dev, now)
            if now >= dev.next_due:
                self._produce(dev, now)
                dev.next_due = now + dev.window_s
                sent += 1
        self._maybe_auto_scenario(dt)
        return sent

    def run(self, stop: threading.Event, duration_s: float = 0) -> None:
        end = time.monotonic() + duration_s if duration_s else None
        last_report = time.monotonic()
        sent = 0
        while not stop.is_set() and (end is None or time.monotonic() < end):
            sent += self.tick()
            if time.monotonic() - last_report >= 60:
                active = sum(1 for d in self.devices.values() if d.scenario)
                log.info("simulator heartbeat", extra={"devices": len(self.devices), "published_last_min": sent,
                                                        "active_scenarios": active})
                sent, last_report = 0, time.monotonic()
            stop.wait(0.5)

