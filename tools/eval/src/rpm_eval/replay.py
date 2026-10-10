"""Offline replay: the real simulator and the real stream processor, wired in memory on a virtual clock.

A day of the cohort runs in minutes against ground truth, so alert burden, sensitivity, time-to-detect and PPV can
be measured and rule changes compared on identical data. Ground truth comes from two places: the training data's
own deterioration episodes (the `event_severity_fraction` of each synthetic patient's wearable stream, which the
platform never sees), and scenarios injected at known times into patients whose own data is stable.
Infrastructure (MQTT, SQS, Postgres, care-api) is replaced by in-memory equivalents with the same semantics;
the clinical logic under test is the production code, unmodified.

Alert model: one active alert per patient (as care-api's AlertStore). Alert-worthy events while it is open attach,
and only a severity increase re-pages. A simulated clinician resolves an alert once the patient has produced no
alert-worthy event for `resolve_after_s`: an alert is not closed while the patient is still unwell, so one long
deterioration counts as one alert. This is what turns raw rule findings into the alert and page counts a care team
would experience.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from rpm_core.cohort import CohortMember
from rpm_core.contracts import SEVERITY_RANK
from rpm_simulator import engine as sim_engine
from rpm_simulator.engine import CLINICAL_SCENARIOS, Simulator
from rpm_simulator.scenarios import SCENARIOS
from stream_processor.processor import Processor
from stream_processor.registry import PatientRef
from stream_processor.state import LocalState

NEGATIVE_SCENARIOS = ("motion_artifact", "device_offline")  # must not alert
DATASET_EPISODE = "dataset_episode"   # a deterioration that is in the patient's own training data
EPISODE_SEVERITY = 0.5                # severity fraction at or above which the data counts as deteriorated


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
class Registry:
    def __init__(self, refs: dict[str, PatientRef]):
        self.refs = refs

    def resolve(self, device_id):
        return self.refs.get(device_id)

    def invalidate(self, _patient_id):
        pass


class EventSink:
    """Stands in for Postgres and care-api: keeps every emitted clinical event and counts the vitals rows."""

    def __init__(self):
        self.events: list = []
        self.windows = 0

    def write(self, vitals, _status, _events, _rule_states=None, _ehr=None):
        self.windows += len(vitals)

    def emit(self, events):
        self.events.extend(events)


def _record(topic: str, payload: str, clock: VirtualClock) -> dict:
    """What the IoT rule adds to a device message."""
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


def _stable(m: CohortMember) -> bool:
    return m.record is None or m.record.trajectory == "stable"


def schedule(members: list[CohortMember], hours: float, seed: str, per_type_home: int = 3,
             per_type_icu: int = 2, negatives_home: int = 5) -> list[Case]:
    """Scenarios injected at known times. Home scenarios go only to patients whose own data is stable, so an
    injected deterioration is never confused with one that is already in the data."""
    rng = random.Random(f"{seed}:schedule")
    home = [m for m in members if m.care_setting == "home" and _stable(m)]
    icu = [m for m in members if m.care_setting == "icu"]
    rng.shuffle(home)
    rng.shuffle(icu)
    plan: list[tuple[CohortMember, str]] = []
    for s in CLINICAL_SCENARIOS:
        plan += [(home.pop(), s) for _ in range(per_type_home) if home]
        plan += [(icu.pop(), s) for _ in range(per_type_icu) if icu]
    for s in NEGATIVE_SCENARIOS:
        plan += [(home.pop(), s) for _ in range(negatives_home) if home]
    cases = []
    for m, s in plan:
        spec = SCENARIOS[s]
        onset = rng.uniform(1.5 * 3600, max(2 * 3600, (hours - 1) * 3600))  # after rule warm-up, before the end
        cases.append(Case(m.patient_id, m.device_id, m.care_setting, s, onset, spec.ramp_s, spec.default_duration_s))
    return cases


def dataset_truth(members: list[CohortMember], start: datetime, hours: float) -> dict[str, str]:
    """What each home patient's own data says about the replayed hours, from the severity the data carries:
    `deteriorated` (severity reaches EPISODE_SEVERITY), `mild` (above zero but below it), `stable` (zero throughout).
    MIMIC patients have no severity column, so they are `unlabelled`."""
    out = {}
    for m in members:
        if m.care_setting != "home" or not m.discharge_at:
            continue
        offset_h = (start.replace(tzinfo=None) - m.discharge_at).total_seconds() / 3600
        sev = [v["severity"] for h, v in m.points if "severity" in v and offset_h <= h <= offset_h + hours]
        out[m.patient_id] = "unlabelled" if not sev else "deteriorated" if max(sev) >= EPISODE_SEVERITY else \
            "mild" if max(sev) > 0 else "stable"
    return out


def dataset_episodes(members: list[CohortMember], start: datetime, hours: float, min_s: float = 1800) -> list[Case]:
    """Deteriorations that are in the training data itself: stretches of a patient's wearable stream where the
    data's severity fraction is at or above EPISODE_SEVERITY, placed on the replay clock. Only episodes that begin
    after the first hour count (earlier ones started before monitoring did)."""
    cases = []
    for m in members:
        if m.care_setting != "home" or not m.discharge_at:
            continue
        offset_h = (start.replace(tzinfo=None) - m.discharge_at).total_seconds() / 3600
        begin = None
        for h, v in [*m.points, (float("inf"), {"severity": 0.0})]:
            sick = v.get("severity", 0.0) >= EPISODE_SEVERITY
            if sick and begin is None:
                begin = h
            elif not sick and begin is not None:
                onset, end = (begin - offset_h) * 3600, (min(h, m.points[-1][0]) - offset_h) * 3600
                if 3600 <= onset < hours * 3600 and end - onset >= min_s:
                    cases.append(Case(m.patient_id, m.device_id, "home", DATASET_EPISODE, onset, 0.0, end - onset))
                begin = None
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
    last_s: float = 0.0     # time of the latest alert-worthy event attached to it


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
        if a and t - a.last_s >= self.resolve_after_s:   # quiet for long enough: the clinician has resolved it
            self.active.pop(ev.patient_id)
            a = None
        sev = ev.severity if ev.severity in SEVERITY_RANK else "medium"
        if a is None:
            a = Alert(ev.patient_id, ev.context or "home", t, sev, ev.type, {ev.type}, t)
            self.active[ev.patient_id] = a
            self.alerts.append(a)
            self.pages.append((t, ev.patient_id, "opened"))
        else:
            a.types.add(ev.type)
            a.last_s = t
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
    truth: dict[str, str] = field(default_factory=dict)   # home patient -> what their own data says (see below)


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


def run(members: list[CohortMember], *, hours: float = 24, seed: str = "rpm-v1", step_s: float = 5,
        resolve_after_s: float = 1800, cases: list[Case] | None = None, progress=None) -> ReplayResult:
    start = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    clock = VirtualClock(start)
    saved = (sim_engine.time, sim_engine.utcnow)   # the simulator reads time through these module globals
    sim_engine.time, sim_engine.utcnow = clock, clock.utcnow
    try:
        return _run(members, clock, start, hours, seed, step_s, resolve_after_s, cases, progress)
    finally:
        sim_engine.time, sim_engine.utcnow = saved


def _run(members, clock, start, hours, seed, step_s, resolve_after_s, cases, progress):
    refs = {m.device_id: PatientRef(m.patient_id, m.care_setting, m.condition, m.hypercapnic, False, {})
            for m in members}
    sink = EventSink()
    proc = Processor(Registry(refs), LocalState(pool=None, clock=clock.time), sink, sink, clock=clock.utcnow)
    records: list[dict] = []

    def publish(_key: str, topic: str, payload: str) -> None:
        records.append(_record(topic, payload, clock))

    sim = Simulator(members, publish, window_s=30, speed=1.0, seed=seed, auto_scenarios_per_hour=0,
                    icu_window_s=15)
    injected = cases if cases is not None else schedule(members, hours, seed)
    cases = injected + dataset_episodes(members, start, hours)
    pending = sorted(injected, key=lambda c: c.onset_s)
    clinician = ClinicianModel(resolve_after_s)
    end = hours * 3600
    seen_events = 0
    next_report = 3600.0
    while clock.t < end:
        while pending and pending[0].onset_s <= clock.t:
            c = pending.pop(0)
            sim.start_scenario(c.patient_id, c.scenario)
        sim.tick(step_s)
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
    return ReplayResult(hours, {"home": sum(m.care_setting == "home" for m in members),
                                "icu": sum(m.care_setting == "icu" for m in members)},
                        cases, sink.events, clinician.alerts, clinician.pages, sink.windows, start,
                        dataset_truth(members, start, hours))


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
    injected_pids = {c.patient_id for c in r.cases if c.scenario != DATASET_EPISODE}
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

    # Patient-level agreement with the training data's own labels (home patients)
    by_truth: dict[str, dict] = {}
    for label in ("deteriorated", "mild", "stable", "unlabelled"):
        pids = [p for p, t in r.truth.items() if t == label and p not in injected_pids]
        n_alerts = sum(len(alerts_by_patient[p]) for p in pids)
        by_truth[label] = {"patients": len(pids), "patients_alerted": sum(1 for p in pids if alerts_by_patient[p]),
                           "alerts": n_alerts, "alerts_per_patient_day": per_day(n_alerts, len(pids))}

    detected = [x for x in case_rows if x["scenario"] not in NEGATIVE_SCENARIOS]
    negatives = [x for x in case_rows if x["scenario"] in NEGATIVE_SCENARIOS]
    return {"hours": r.hours, "patients": r.patients, "windows_scored": r.windows,
            "sensitivity": {"detected": sum(x["detected"] for x in detected), "cases": len(detected),
                            "negative_scenarios_alerting": sum(x["detected"] for x in negatives)},
            "missed": [x for x in detected if not x["detected"]],
            "alert_worthy_events": sum(1 for e in r.events if e.requires_alert),
            "burden": burden, "against_training_data_labels": by_truth,
            "scenarios": {f"{ctx}/{s}": summarise(rows) for (ctx, s), rows in sorted(by_type.items())},
            "cases": case_rows}
