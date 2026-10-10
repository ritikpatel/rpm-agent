"""Offline replay: the real simulator, ICU gateway and stream-engine processor, wired in memory on a virtual clock.

A day of the 149-device cohort runs in minutes with deterministic ground truth (which patient deteriorates, when,
and how), so alarm burden, sensitivity, time-to-detect and PPV can be measured and rule changes compared on
identical data. Infrastructure (MQTT, SQS, Redis, Postgres) is replaced by in-memory equivalents with the
same semantics; the clinical logic under test is the production code, unmodified.

Alert model: one active alert per patient (as care_api.alerts.AlertStore). A simulated clinician resolves each alert
`resolve_after_s` after it opened; alert-worthy events while it is open attach, and only a severity increase
re-pages. This is what turns raw rule findings into the alert and page counts a care team would experience.
"""

from __future__ import annotations

import random
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rpm_core.contracts import SEVERITY_RANK
from rpm_icu_gateway import gateway as gw_module
from rpm_icu_gateway import hl7
from rpm_icu_gateway import outbox as outbox_module
from rpm_icu_gateway.gateway import Gateway
from rpm_icu_gateway.outbox import Outbox
from rpm_core.mimic import CohortMember
from rpm_simulator import engine as sim_engine
from rpm_simulator.engine import CLINICAL_SCENARIOS, Simulator
from rpm_simulator.hl7_monitor import observations
from rpm_simulator.scenarios import SCENARIOS
from stream_processor.processor import Processor
from stream_processor.registry import PatientRef

GATEWAY = "icu-gw-01"
NEGATIVE_SCENARIOS = ("motion_artifact", "device_offline")  # must not alert


# ---------------------------------------------------------------------- virtual time
class VirtualClock:
    def __init__(self, start: datetime):
        self.start, self.t = start, 0.0

    def monotonic(self) -> float:
        return self.t

    def time(self) -> float:
        return self.start.timestamp() + self.t

    def utcnow(self) -> datetime:
        return self.start + timedelta(seconds=self.t)

    def sleep(self, _s: float) -> None:  # pragma: no cover - never called in replay
        raise RuntimeError("replay code must not sleep")


# ---------------------------------------------------------------------- in-memory stand-ins
class MemoryState:
    """RedisState semantics (dedupe, suppression keys with TTL, latest vitals, rule state) on the virtual clock."""

    def __init__(self, clock: VirtualClock):
        self.clock = clock
        self.kv: dict[str, tuple[object, float | None]] = {}
        self.latest: dict[str, dict] = {}
        self.seen_ids: set[str] = set()

    def _get(self, key):
        v = self.kv.get(key)
        if v is None or (v[1] is not None and v[1] <= self.clock.t):
            return None
        return v[0]

    def seen(self, ids):
        return {i for i in ids if i in self.seen_ids}

    def mark_seen(self, ids):
        self.seen_ids.update(ids)

    def swap_max(self, key, value):
        prev = self._get(key)
        if prev is None or value > prev:
            self.kv[key] = (value, None)
        return prev

    def update_latest(self, pid, values):
        self.latest.setdefault(pid, {}).update(values)

    def get_latest(self, pid):
        return dict(self.latest.get(pid, {}))

    def get_rule_state(self, pid):
        return self._get(f"rules:{pid}")

    def set_rule_state(self, pid, raw):
        self.kv[f"rules:{pid}"] = (raw, None)

    def should_emit(self, pid, etype, rank, ttl):
        if ttl <= 0:
            return True
        key = f"sup:{pid}:{etype}"
        cur = self._get(key)
        if cur is None or rank > cur:
            self.kv[key] = (rank, self.clock.t + ttl)
            return True
        return False

    def release_emit(self, slots):
        for pid, etype in slots:
            self.kv.pop(f"sup:{pid}:{etype}", None)

    def publish_live(self, _msgs):
        pass


class Registry:
    def __init__(self, refs: dict[str, PatientRef]):
        self.refs = refs

    def resolve(self, device_id):
        return self.refs.get(device_id)


class EventSink:
    """Stands in for Postgres + SQS: keeps every emitted clinical event and the vitals rows."""

    def __init__(self):
        self.events: list = []
        self.windows = 0

    def write(self, vitals, _status, _events):
        self.windows += len(vitals)

    def emit(self, events):
        self.events.extend(events)


class CaptureUplink:
    """ICU gateway uplink: turns its publishes into ingest records stamped with the gateway thing."""

    def __init__(self, clock: VirtualClock, records: list):
        self.clock, self.records = clock, records

    @property
    def connected(self) -> bool:
        return True

    def publish(self, topic: str, payload: str) -> bool:
        self.records.append(_record(topic, payload, self.clock))
        return True

    def close(self) -> None:
        pass


def _record(topic: str, payload: str, clock: VirtualClock) -> dict:
    import json
    parts = topic.split("/")
    return {**json.loads(payload), "thing": parts[3], "kind": parts[4], "ingest_ts_ms": int(clock.time() * 1000)}


# ---------------------------------------------------------------------- ground truth
@dataclass
class Case:
    patient_id: str
    device_id: str
    context: str
    scenario: str
    onset_s: float
    ramp_s: float
    duration_s: float

    @property
    def window(self) -> tuple[float, float]:
        return self.onset_s, self.onset_s + self.duration_s + 300  # 5 min after the hold for late detection


def schedule(members: list[CohortMember], hours: float, seed: str, per_type_home: int = 4,
             per_type_icu: int = 2, negatives_home: int = 5) -> list[Case]:
    rng = random.Random(f"{seed}:schedule")
    home = [m for m in members if m.care_setting == "home"]
    icu = [m for m in members if m.care_setting == "icu"]
    rng.shuffle(home)
    rng.shuffle(icu)
    plan: list[tuple[CohortMember, str]] = []
    for s in CLINICAL_SCENARIOS:
        plan += [(home.pop(), s) for _ in range(per_type_home)] + [(icu.pop(), s) for _ in range(per_type_icu)]
    for s in NEGATIVE_SCENARIOS:
        plan += [(home.pop(), s) for _ in range(negatives_home)]
    cases = []
    for m, s in plan:
        spec = SCENARIOS[s]
        onset = rng.uniform(1.5 * 3600, max(2 * 3600, (hours - 1) * 3600))  # after rule warm-up, before the end
        cases.append(Case(m.patient_id, m.device_id, m.care_setting, s, onset, spec.ramp_s, spec.default_duration_s))
    return cases


# ---------------------------------------------------------------------- alert model
@dataclass
class Alert:
    patient_id: str
    context: str
    opened_s: float
    severity: str
    first_type: str
    types: set[str] = field(default_factory=set)


class ClinicianModel:
    def __init__(self, resolve_after_s: float):
        self.resolve_after_s = resolve_after_s
        self.active: dict[str, Alert] = {}
        self.alerts: list[Alert] = []
        self.pages: list[tuple[float, str, str]] = []  # (t, patient, reason)

    def on_event(self, ev, t: float) -> None:
        if not ev.requires_alert:
            return
        a = self.active.get(ev.patient_id)
        if a and t - a.opened_s >= self.resolve_after_s:
            a = self.active.pop(ev.patient_id)
            a = None
        sev = ev.severity if ev.severity in SEVERITY_RANK else "medium"
        if a is None:
            a = Alert(ev.patient_id, ev.context or "home", t, sev, ev.type, {ev.type})
            self.active[ev.patient_id] = a
            self.alerts.append(a)
            self.pages.append((t, ev.patient_id, "opened"))
        else:
            a.types.add(ev.type)
            if SEVERITY_RANK[sev] > SEVERITY_RANK[a.severity]:
                a.severity = sev
                self.pages.append((t, ev.patient_id, "severity_raised"))


# ---------------------------------------------------------------------- the run
@dataclass
class ReplayResult:
    hours: float
    patients: dict[str, int]
    cases: list[Case]
    events: list
    alerts: list[Alert]
    pages: list[tuple[float, str, str]]
    windows: int
    start: datetime


def apply_overrides(overrides: dict[str, str]) -> dict:
    """Rule knobs for sweeps: module constants (BASELINE_STEP=3) or per-setting thresholds (home.news2_persist_s=600).
    Returns what to restore."""
    from dataclasses import replace

    from rpm_core.features import rules
    from stream_processor import processor

    saved = {"default_thresholds": processor.default_thresholds}
    per_ctx: dict[str, dict] = {}
    for key, raw in overrides.items():
        value = float(raw) if "." in raw else int(raw)
        if "." in key:
            ctx, field_name = key.split(".", 1)
            per_ctx.setdefault(ctx, {})[field_name] = value
        else:
            saved[key] = getattr(rules, key)
            setattr(rules, key, value)
    if per_ctx:
        base = rules.default_thresholds
        processor.default_thresholds = lambda cond, ctx: replace(base(cond, ctx), **per_ctx.get(ctx, {}))
    return saved


def restore_overrides(saved: dict) -> None:
    from rpm_core.features import rules
    from stream_processor import processor

    processor.default_thresholds = saved.pop("default_thresholds")
    for k, v in saved.items():
        setattr(rules, k, v)


def run(members: list[CohortMember], series: dict, *, hours: float = 24, seed: str = "rpm-v1", step_s: float = 5,
        resolve_after_s: float = 1800, icu_via_gateway: bool = True, cases: list[Case] | None = None,
        progress=None) -> ReplayResult:
    start = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    clock = VirtualClock(start)
    # the code under test reads time through these module globals
    saved = (sim_engine.time, sim_engine.utcnow, gw_module.time, gw_module.utcnow, outbox_module.time)
    sim_engine.time = gw_module.time = outbox_module.time = clock
    sim_engine.utcnow = gw_module.utcnow = clock.utcnow
    try:
        return _run(members, series, clock, start, hours, seed, step_s, resolve_after_s, icu_via_gateway, cases,
                    progress)
    finally:
        sim_engine.time, sim_engine.utcnow, gw_module.time, gw_module.utcnow, outbox_module.time = saved


def _run(members, series, clock, start, hours, seed, step_s, resolve_after_s, icu_via_gateway, cases, progress):
    refs = {m.device_id: PatientRef(m.patient_id, m.care_setting, m.condition, m.hypercapnic, False, {},
                                    GATEWAY if (icu_via_gateway and m.care_setting == "icu") else None)
            for m in members}
    sink = EventSink()
    proc = Processor(Registry(refs), MemoryState(clock), sink, sink, clock=clock.utcnow)
    records: list[dict] = []
    icu_ids = {m.device_id for m in members if m.care_setting == "icu"}

    tmp = tempfile.TemporaryDirectory()
    gateway = None
    if icu_via_gateway:
        gateway = Gateway(GATEWAY, Outbox(Path(tmp.name) / "gw.db"), window_s=15, clock=clock.utcnow)
        gateway.uplink = CaptureUplink(clock, records)
        gateway.on_config(_bed_config(members).encode())
    control = {"n": 0}

    def publish(key: str, topic: str, payload: str) -> None:
        if gateway is not None and key in icu_ids:
            if topic.endswith("/telemetry"):
                import json
                t = json.loads(payload)
                obs = observations(t["metrics"])
                if obs:
                    control["n"] += 1
                    gateway.on_hl7(hl7.oru(key, f"R{control['n']}", clock.utcnow(), obs))
            return
        records.append(_record(topic, payload, clock))

    sim = Simulator(members, series, publish, window_s=30, speed=1.0, seed=seed, tz_offset_h=4.0,
                    auto_scenarios_per_hour=0, icu_period_s=5 if gateway else None)
    cases = cases if cases is not None else schedule(members, hours, seed)
    pending = sorted(cases, key=lambda c: c.onset_s)
    clinician = ClinicianModel(resolve_after_s)
    end = hours * 3600
    seen_events = 0
    next_report = 3600.0
    while clock.t < end:
        while pending and pending[0].onset_s <= clock.t:
            c = pending.pop(0)
            sim.start_scenario(c.patient_id, c.scenario)
        sim.tick(step_s)
        if gateway is not None:
            gateway.tick()
            gateway.drain()
        if records:
            proc.handle_batch(records)
            records.clear()
        for ev in sink.events[seen_events:]:
            clinician.on_event(ev, clock.t)
        seen_events = len(sink.events)
        clock.t += step_s
        if progress and clock.t >= next_report:
            progress(clock.t / 3600, len(sink.events), len(clinician.alerts))
            next_report += 3600
    tmp.cleanup()
    return ReplayResult(hours, {"home": sum(m.care_setting == "home" for m in members),
                                "icu": sum(m.care_setting == "icu" for m in members)},
                        cases, sink.events, clinician.alerts, clinician.pages, sink.windows, start)


def _bed_config(members: list[CohortMember]) -> str:
    from rpm_core.contracts import GatewayConfig
    return GatewayConfig(gateway_id=GATEWAY, beds={
        m.device_id: {"spo2_scale": 2 if m.hypercapnic else 1} for m in members if m.care_setting == "icu"}).to_json()


# ---------------------------------------------------------------------- metrics
def metrics(r: ReplayResult) -> dict:
    t0 = r.start
    ev_by_patient: dict[str, list] = defaultdict(list)
    for ev in r.events:
        if ev.requires_alert:
            ev_by_patient[ev.patient_id].append(((ev.detected_at - t0).total_seconds(), ev))
    alerts_by_patient: dict[str, list[Alert]] = defaultdict(list)
    for a in r.alerts:
        alerts_by_patient[a.patient_id].append(a)
    pages_by_patient: dict[str, list[float]] = defaultdict(list)
    for t, pid, _why in r.pages:
        pages_by_patient[pid].append(t)

    case_rows, by_type = [], defaultdict(list)
    case_windows: dict[str, tuple[float, float]] = {}
    for c in r.cases:
        lo, hi = c.window
        case_windows[c.patient_id] = (lo, hi)
        first_event = next((t for t, _ in sorted(ev_by_patient[c.patient_id], key=lambda x: x[0]) if lo <= t <= hi),
                           None)
        first_page = next((t for t in sorted(pages_by_patient[c.patient_id]) if lo <= t <= hi), None)
        row = {"patient_id": c.patient_id, "context": c.context, "scenario": c.scenario,
               "detected": first_event is not None, "paged": first_page is not None,
               "time_to_detect_s": None if first_event is None else round(first_event - lo, 1),
               "time_to_page_s": None if first_page is None else round(first_page - lo, 1)}
        case_rows.append(row)
        by_type[(c.context, c.scenario)].append(row)

    def per_day(n: int, patients: int) -> float:
        return round(n / max(1, patients) / (r.hours / 24), 3)

    case_pids = {c.patient_id for c in r.cases}
    burden = {}
    for ctx in ("home", "icu"):
        ctx_alerts = [a for a in r.alerts if a.context == ctx]
        ctrl_alerts = [a for a in ctx_alerts if a.patient_id not in case_pids]
        ctrl_patients = r.patients[ctx] - sum(1 for c in r.cases if c.context == ctx)
        in_case = [a for a in ctx_alerts if a.patient_id in case_windows
                   and case_windows[a.patient_id][0] <= a.opened_s <= case_windows[a.patient_id][1]]
        ctx_pages = [p for p in r.pages if (p[1] in {a.patient_id for a in ctx_alerts})]
        by_rule = defaultdict(int)
        for a in ctrl_alerts:
            by_rule[a.first_type] += 1
        steady = [a for a in ctx_alerts if a.opened_s >= 3600]  # after every patient has a 1-h NEWS2 baseline
        burden[ctx] = {
            "alerts_per_patient_day": per_day(len(ctx_alerts), r.patients[ctx]),
            "steady_state_alerts_per_patient_day": round(len(steady) / max(1, r.patients[ctx])
                                                         / ((r.hours - 1) / 24), 3),
            "control_alerts_per_patient_day": per_day(len(ctrl_alerts), ctrl_patients),
            "pages_per_patient_day": per_day(len(ctx_pages), r.patients[ctx]),
            "alerts": len(ctx_alerts), "alerts_during_scenarios": len(in_case),
            "ppv": round(len(in_case) / len(ctx_alerts), 3) if ctx_alerts else None,
            "control_alerts_by_first_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
        }

    def summarise(rows):
        tt = sorted(x["time_to_detect_s"] for x in rows if x["time_to_detect_s"] is not None)
        return {"n": len(rows), "detected": sum(x["detected"] for x in rows), "paged": sum(x["paged"] for x in rows),
                "median_time_to_detect_s": tt[len(tt) // 2] if tt else None}

    detected = [x for x in case_rows if x["scenario"] not in NEGATIVE_SCENARIOS]
    negatives = [x for x in case_rows if x["scenario"] in NEGATIVE_SCENARIOS]
    return {"hours": r.hours, "patients": r.patients, "windows_scored": r.windows,
            "sensitivity": {"detected": sum(x["detected"] for x in detected), "cases": len(detected),
                            "negative_scenarios_alerting": sum(x["detected"] for x in negatives)},
            "missed": [x for x in detected if not x["detected"]],
            "alert_worthy_events": sum(1 for e in r.events if e.requires_alert),
            "burden": burden,
            "scenarios": {f"{ctx}/{s}": summarise(rows) for (ctx, s), rows in sorted(by_type.items())},
            "cases": case_rows}
