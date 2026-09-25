"""Deterministic clinical rules: the safety net that fires regardless of model or LLM availability.

`evaluate()` is pure: callers pass the previous `RuleState` and persist the returned one (the stream engine
keeps it in Redis). Duplicate suppression across windows is the caller's job; rules report every window in
which a condition holds.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta

from .news2 import BAND_HIGH, BAND_LOW_MEDIUM, BAND_MEDIUM, News2Result

NEWS2_TREND_WINDOW = timedelta(minutes=60)
NEWS2_TREND_DELTA = 3


@dataclass(frozen=True)
class Thresholds:
    hr_high: float = 120
    hr_low: float = 45
    spo2_low: float = 90
    spo2_critical: float = 85
    sustain_s: int = 120
    hr_hysteresis: float = 5
    min_quality: float = 0.6

    def merged(self, overrides: dict | None) -> Thresholds:
        if not overrides:
            return self
        known = {k: v for k, v in overrides.items() if k in self.__dataclass_fields__ and v is not None}
        return replace(self, **known)


def default_thresholds(condition: str | None, context: str) -> Thresholds:
    """Condition- and setting-aware defaults. Clinicians/agents override per patient via the device shadow."""
    t = Thresholds(hr_high=130, hr_low=40, sustain_s=60) if context == "icu" else Thresholds()
    cond = (condition or "").upper()
    if cond == "COPD":  # hypercapnic target 88-92 %
        t = replace(t, spo2_low=88, spo2_critical=84)
    elif cond in ("CHF", "SEPSIS") and context == "home":
        t = replace(t, hr_high=110)
    return t


@dataclass(frozen=True)
class Snapshot:
    window_start: datetime
    ts: datetime  # window_end = event time
    heart_rate: float | None = None
    hr_quality: float | None = None
    spo2: float | None = None
    spo2_quality: float | None = None
    news2: News2Result | None = None


@dataclass
class RuleState:
    sustain_since: dict[str, str] = field(default_factory=dict)
    news2_history: list[tuple[str, int]] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes | None) -> RuleState:
        if not raw:
            return cls()
        d = json.loads(raw)
        return cls(sustain_since=d.get("sustain_since", {}),
                   news2_history=[tuple(x) for x in d.get("news2_history", [])])


@dataclass(frozen=True)
class Finding:
    type: str
    severity: str
    rule_id: str
    summary: str
    requires_alert: bool
    evidence: dict = field(default_factory=dict)


def _ok(quality: float | None, th: Thresholds) -> bool:
    return quality is None or quality >= th.min_quality


def _sustained(
    key: str, breached: bool, cleared: bool, snap: Snapshot, state: RuleState, sustain_s: int
) -> float | None:
    """Track how long a condition has held. Returns the sustained duration (s) once >= sustain_s."""
    if breached:
        since = datetime.fromisoformat(state.sustain_since.setdefault(key, snap.window_start.isoformat()))
        held = (snap.ts - since).total_seconds()
        return held if held >= sustain_s else None
    if cleared:
        state.sustain_since.pop(key, None)
    return None


def evaluate(snap: Snapshot, th: Thresholds, prior: RuleState | None = None) -> tuple[list[Finding], RuleState]:
    state = RuleState(dict((prior or RuleState()).sustain_since), list((prior or RuleState()).news2_history))
    out: list[Finding] = []

    # --- NEWS2 bands (single most severe band) and trend
    n = snap.news2
    if n is not None:
        detail = n.explain()
        ev = {"news2_total": n.total, "params": n.params, "partial": n.partial}
        if n.band == BAND_HIGH:
            out.append(Finding("NEWS2_HIGH", "high", "news2.high", detail, True, ev))
        elif n.band == BAND_MEDIUM:
            out.append(Finding("NEWS2_MEDIUM", "medium", "news2.medium", detail, True, ev))
        elif n.band == BAND_LOW_MEDIUM:
            out.append(Finding("NEWS2_RED_PARAM", "medium", "news2.red_param", detail, True,
                               {**ev, "red_params": list(n.red_params)}))

        cutoff = snap.ts - NEWS2_TREND_WINDOW
        hist = [(t, s) for t, s in state.news2_history if datetime.fromisoformat(t) >= cutoff]
        if hist and n.total - min(s for _, s in hist) >= NEWS2_TREND_DELTA:
            base = min(s for _, s in hist)
            out.append(Finding("NEWS2_RISING", "low", "news2.rising",
                               f"NEWS2 rose from {base} to {n.total} within 60 min", False,
                               {"from": base, "to": n.total}))
        hist.append((snap.ts.isoformat(), n.total))
        state.news2_history = hist[-240:]

    # --- SpO2 (spot checks are sparse, so no sustain requirement)
    if snap.spo2 is not None and _ok(snap.spo2_quality, th):
        if snap.spo2 < th.spo2_critical:
            out.append(Finding("SPO2_CRITICAL", "critical", "spo2.critical",
                               f"SpO2 {snap.spo2:.1f}% below critical threshold {th.spo2_critical:.0f}%", True,
                               {"spo2": snap.spo2, "threshold": th.spo2_critical}))
        elif snap.spo2 < th.spo2_low:
            out.append(Finding("SPO2_LOW", "medium", "spo2.low",
                               f"SpO2 {snap.spo2:.1f}% below threshold {th.spo2_low:.0f}%", True,
                               {"spo2": snap.spo2, "threshold": th.spo2_low}))

    # --- Heart rate, sustained (motion-corrupted windows neither start nor clear a run)
    if snap.heart_rate is not None and _ok(snap.hr_quality, th):
        hr = snap.heart_rate
        held = _sustained("hr_high", hr > th.hr_high, hr < th.hr_high - th.hr_hysteresis, snap, state, th.sustain_s)
        if held is not None:
            out.append(Finding("HR_SUSTAINED_HIGH", "medium", "hr.sustained_high",
                               f"HR {hr:.0f}/min above {th.hr_high:.0f} for {held / 60:.1f} min", True,
                               {"heart_rate": hr, "threshold": th.hr_high, "sustained_s": held}))
        held = _sustained("hr_low", hr < th.hr_low, hr > th.hr_low + th.hr_hysteresis, snap, state, th.sustain_s)
        if held is not None:
            out.append(Finding("HR_SUSTAINED_LOW", "medium", "hr.sustained_low",
                               f"HR {hr:.0f}/min below {th.hr_low:.0f} for {held / 60:.1f} min", True,
                               {"heart_rate": hr, "threshold": th.hr_low, "sustained_s": held}))

    # A NEWS2 "red parameter" finding adds nothing when a dedicated rule already reports that parameter
    # (e.g. SpO2 87 % raises SPO2_LOW); dropping it avoids double alerts for the same reading.
    covered = {p for p, kinds in _PARAM_RULES.items() if kinds & {f.type for f in out}}
    if n is not None and n.band == BAND_LOW_MEDIUM and set(n.red_params) <= covered:
        out = [f for f in out if f.type != "NEWS2_RED_PARAM"]
    return out, state


_PARAM_RULES = {
    "spo2": {"SPO2_LOW", "SPO2_CRITICAL"},
    "heart_rate": {"HR_SUSTAINED_HIGH", "HR_SUSTAINED_LOW"},
}
