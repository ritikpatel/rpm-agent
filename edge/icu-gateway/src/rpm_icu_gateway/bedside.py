"""Per-bed state at the gateway: windows raw monitor observations into rpm.telemetry.v1 summaries and runs bedside
NEWS2 + alarms locally, so an ICU alarm still sounds at the nurse station when the cloud link is down.

The cloud recomputes NEWS2 from the telemetry itself (one implementation, rpm_features); the gateway's copy only
drives local alarms and the EDGE_ALERT event, and is marked in edge.flags so the two can be compared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from rpm_contracts import (
    Aggregate,
    DeviceEvent,
    DeviceEventTs,
    DeviceInfo,
    EdgeInfo,
    Metrics,
    Scalar,
    SpO2,
    Telemetry,
    TelemetryTs,
)
from rpm_features import News2Input, News2Result, compute_news2, consciousness_from_gcs

SOURCE = "icu_gateway"

# Bedside alarm ladder (RCP NEWS2 escalation bands + single-parameter limits monitors alarm on anyway).
ALARM_RULES = (
    # (rule, severity, predicate on (news2, metrics))
    ("NEWS2_HIGH", "high", lambda n, m: n is not None and n.total >= 7),
    ("SPO2_CRITICAL", "critical", lambda n, m: m.get("spo2") is not None and m["spo2"] < 85),
    ("HR_EXTREME", "high", lambda n, m: m.get("heart_rate") is not None and not 40 <= m["heart_rate"] <= 140),
    ("SBP_LOW", "high", lambda n, m: m.get("sbp") is not None and m["sbp"] <= 90),
    ("NEWS2_RED_PARAM", "medium", lambda n, m: n is not None and bool(n.red_params)),
)
REANNOUNCE = timedelta(minutes=10)  # a latched alarm is re-sent to the cloud this often while it persists


@dataclass
class Alarm:
    rule: str
    severity: str
    since: datetime
    last_sent: datetime


@dataclass
class Bed:
    device_id: str
    window_s: int
    hypercapnic: bool = False            # NEWS2 SpO2 scale 2 (set from the cloud's retained gateway config)
    on_o2: bool | None = None
    seq: int = 0
    samples: dict[str, list[float]] = field(default_factory=dict)
    window_start: datetime | None = None
    last_sample: datetime | None = None
    gcs_best: float | None = None
    alarm: Alarm | None = None
    news2: News2Result | None = None
    latest: dict[str, float] = field(default_factory=dict)

    def add(self, metric: str, value: float, at: datetime) -> None:
        if self.window_start is None:
            self.window_start = at
        self.samples.setdefault(metric, []).append(value)
        self.last_sample = at
        if metric == "gcs":
            self.gcs_best = max(self.gcs_best or value, value)

    def due(self, now: datetime) -> bool:
        return self.window_start is not None and now - self.window_start >= timedelta(seconds=self.window_s)


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def close_window(bed: Bed, now: datetime, next_seq) -> tuple[Telemetry, DeviceEvent | None]:
    """Summarise the bed's window into telemetry; evaluate bedside NEWS2 + alarms. Returns (telemetry, alarm event
    or None). Monitor-grade signals, so quality is high unless the monitor itself flagged the value."""
    s, start = bed.samples, bed.window_start or now
    m: dict = {}
    latest: dict[str, float] = {}
    if hr := s.get("heart_rate"):
        m["heart_rate"] = Aggregate(mean=round(_mean(hr), 1), min=min(hr), max=max(hr), n=len(hr), quality=0.99)
        latest["heart_rate"] = _mean(hr)
    if spo2 := s.get("spo2"):
        # the window's lowest reading is what matters clinically for oxygenation
        m["spo2"] = SpO2(value=round(min(spo2), 1), quality=0.98)
        latest["spo2"] = min(spo2)
    for key in ("resp_rate", "sbp", "dbp", "temp", "gcs"):
        if vals := s.get(key):
            v = vals[-1] if key == "gcs" else _mean(vals)
            m[key] = Scalar(value=round(v, 1))
            latest[key] = v
    bed.latest = latest
    news2 = None
    if latest:
        gcs = latest.get("gcs")
        news2 = compute_news2(News2Input(
            resp_rate=latest.get("resp_rate"), spo2=latest.get("spo2"), on_oxygen=bed.on_o2, sbp=latest.get("sbp"),
            heart_rate=latest.get("heart_rate"), temp=latest.get("temp"),
            consciousness=None if gcs is None else consciousness_from_gcs(gcs, bed.gcs_best),
            hypercapnic_resp_failure=bed.hypercapnic))
    bed.news2 = news2
    flags = [f"BEDSIDE_NEWS2_{news2.total}"] if news2 else []
    event = _evaluate_alarm(bed, news2, latest, now, next_seq)
    if bed.alarm:
        flags.append(bed.alarm.rule)
    bed.seq = next_seq(bed.device_id)
    t = Telemetry(device_id=bed.device_id, seq=bed.seq, source=SOURCE, context="icu",
                  ts=TelemetryTs(window_start=start, window_end=now, edge_sent=now),
                  metrics=Metrics(**m), edge=EdgeInfo(flags=flags, profile="critical"),
                  device=DeviceInfo(fw="rpm-icu-gw-0.3.0"))
    bed.samples, bed.window_start = {}, None
    return t, event


def _evaluate_alarm(bed: Bed, news2: News2Result | None, latest: dict, now: datetime, next_seq) -> DeviceEvent | None:
    hit = next(((rule, sev) for rule, sev, pred in ALARM_RULES if pred(news2, latest)), None)
    if hit is None:
        bed.alarm = None  # cleared locally; the cloud alert stays open until a clinician resolves it
        return None
    rule, severity = hit
    a = bed.alarm
    if a and a.rule == rule and now - a.last_sent < REANNOUNCE:
        return None  # latched: no re-announce storm every window
    bed.alarm = Alarm(rule, severity, a.since if a and a.rule == rule else now, now)
    return DeviceEvent(device_id=bed.device_id, seq=next_seq(bed.device_id), source=SOURCE, type="EDGE_ALERT",
                       ts=DeviceEventTs(occurred=now, edge_sent=now),
                       data={"rule": rule, "severity": severity, "news2": news2.total if news2 else None,
                             "values": {k: round(v, 1) for k, v in latest.items()}, "origin": "bedside_gateway"})
