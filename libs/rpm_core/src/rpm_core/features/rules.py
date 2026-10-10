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
# Per-patient NEWS2 baseline (ICU): median of the last 4 h, excluding the most recent 15 min so a deterioration in
# progress does not drag its own baseline up. Needs 1 h of history; until then absolute scoring applies.
BASELINE_WINDOW = timedelta(hours=4)
BASELINE_EXCLUDE_RECENT = timedelta(minutes=15)
BASELINE_MIN_HISTORY = timedelta(hours=1)
BASELINE_RISE = 3            # NEWS2_RISING: this much above baseline, even without a band change
BASELINE_STEP = 3            # a band alert needs at least this rise over the patient's own baseline (replay sweep)
BASELINE_CHRONIC_SHARE = 0.2 # a red parameter present in >= 20% of the baseline is known, not new
NEWS2_ALWAYS_ALERT = 9       # catastrophe guard: alerts whatever the baseline
# Heart rate is judged against what the watch says the patient is doing: exertional tachycardia and the
# nocturnal dip are physiology, not deterioration (week-4 replay: 245 of 708 home alerts were exactly that).
HR_HIGH_ACTIVITY_ALLOWANCE = {"light": 10, "moderate": 25, "vigorous": 35}
HR_LOW_SLEEP_ALLOWANCE = 5
EXERTION = ("moderate", "vigorous")
BAND_RANK = {"low": 0, BAND_LOW_MEDIUM: 1, BAND_MEDIUM: 2, BAND_HIGH: 3}


@dataclass(frozen=True)
class Thresholds:
    hr_high: float = 120
    hr_low: float = 45
    spo2_low: float = 90
    spo2_critical: float = 85
    sustain_s: int = 120
    hr_hysteresis: float = 5
    min_quality: float = 0.6
    news2_mode: str = "absolute"  # absolute | baseline (alert on change from the patient's own recent baseline)
    news2_persist_s: int = 0      # a NEWS2 step-up must hold this long: one noisy reading is not a deterioration
    rising_alerts: bool = False   # NEWS2_RISING (>= 3 over baseline) is alert-worthy (ICU) or informational (home)

    def merged(self, overrides: dict | None) -> Thresholds:
        if not overrides:
            return self
        known = {k: v for k, v in overrides.items() if k in self.__dataclass_fields__ and v is not None}
        return replace(self, **known)


def default_thresholds(condition: str | None, context: str) -> Thresholds:
    """Condition- and setting-aware defaults. Clinicians/agents override per patient via the device shadow."""
    # Both settings judge NEWS2 against the patient's own baseline once one exists (1 h of data); home patients
    # often carry a chronically elevated score from their condition (RCP NEWS2: established baseline).
    t = Thresholds(hr_high=130, hr_low=40, sustain_s=60, news2_mode="baseline", news2_persist_s=180,
                   rising_alerts=True) if context == "icu" \
        else Thresholds(news2_mode="baseline", news2_persist_s=600)  # home readings are sparser: confirm over 10 min
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
    activity: str | None = None  # resting | light | moderate | vigorous | sleep (wearables only)


@dataclass
class RuleState:
    sustain_since: dict[str, str] = field(default_factory=dict)
    news2_history: list[tuple[str, int]] = field(default_factory=list)            # 60-min trend (every window)
    baseline: list[tuple[str, int, list[str]]] = field(default_factory=list)      # 4 h, one entry per minute

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes | None) -> RuleState:
        if not raw:
            return cls()
        d = json.loads(raw)
        return cls(sustain_since=d.get("sustain_since", {}),
                   news2_history=[tuple(x) for x in d.get("news2_history", [])],
                   baseline=[(x[0], x[1], list(x[2])) for x in d.get("baseline", [])])


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


@dataclass(frozen=True)
class Baseline:
    total: float
    band: str
    red_params: frozenset[str]


def _baseline(state: RuleState, now: datetime) -> Baseline | None:
    """The patient's own recent NEWS2 (median over 4 h, excluding the last 15 min), or None without 1 h of data."""
    lo, hi = now - BASELINE_WINDOW, now - BASELINE_EXCLUDE_RECENT
    entries = [(datetime.fromisoformat(t), s, r) for t, s, r in state.baseline]
    usable = [(t, s, r) for t, s, r in entries if lo <= t <= hi]
    if not usable or hi - usable[0][0] < BASELINE_MIN_HISTORY - BASELINE_EXCLUDE_RECENT:
        return None
    totals = sorted(s for _, s, _ in usable)
    median = totals[len(totals) // 2]
    counts: dict[str, int] = {}
    for _, _, reds in usable:
        for p in reds:
            counts[p] = counts.get(p, 0) + 1
    chronic = frozenset(p for p, c in counts.items() if c >= len(usable) * BASELINE_CHRONIC_SHARE)
    band = BAND_HIGH if median >= 7 else BAND_MEDIUM if median >= 5 else BAND_LOW_MEDIUM if chronic else "low"
    return Baseline(median, band, chronic)


def _news2_findings(n: News2Result, th: Thresholds, base: Baseline | None, exertion: bool) -> list[Finding]:
    detail = n.explain()
    ev = {"news2_total": n.total, "params": n.params, "partial": n.partial}
    if exertion:
        return []  # NEWS2 is defined on observations at rest; exertional windows are stored, not alerted on
    if n.total >= NEWS2_ALWAYS_ALERT:
        return [Finding("NEWS2_HIGH", "high", "news2.catastrophic", detail, True, ev)]
    if th.news2_mode == "baseline" and base is not None:
        # Judge the patient against themselves: a chronically elevated score is known (and in ICU the team is at
        # the bedside); what needs escalating is a step up of >= 2 into a worse band, or a new red parameter.
        ev |= {"baseline_news2": base.total, "baseline_band": base.band}
        rise = n.total - base.total
        new_reds = [p for p in n.red_params if p not in base.red_params]
        if n.band == BAND_HIGH and rise >= BASELINE_STEP:
            return [Finding("NEWS2_HIGH", "high", "news2.high_vs_baseline",
                            f"{detail} (baseline {base.total:.0f})", True, ev)]
        if n.band == BAND_MEDIUM and rise >= BASELINE_STEP:
            return [Finding("NEWS2_MEDIUM", "medium", "news2.medium_vs_baseline",
                            f"{detail} (baseline {base.total:.0f})", True, ev)]
        if new_reds:
            return [Finding("NEWS2_RED_PARAM", "medium", "news2.new_red_param",
                            f"{detail}; new red parameter(s): {', '.join(new_reds)}", True,
                            {**ev, "red_params": new_reds})]
        return []
    if n.band == BAND_HIGH:
        return [Finding("NEWS2_HIGH", "high", "news2.high", detail, True, ev)]
    if n.band == BAND_MEDIUM:
        return [Finding("NEWS2_MEDIUM", "medium", "news2.medium", detail, True, ev)]
    if n.band == BAND_LOW_MEDIUM:
        return [Finding("NEWS2_RED_PARAM", "medium", "news2.red_param", detail, True,
                        {**ev, "red_params": list(n.red_params)})]
    return []


def evaluate(snap: Snapshot, th: Thresholds, prior: RuleState | None = None) -> tuple[list[Finding], RuleState]:
    p = prior or RuleState()
    state = RuleState(dict(p.sustain_since), list(p.news2_history), list(p.baseline))
    out: list[Finding] = []
    exertion = snap.activity in EXERTION

    # --- NEWS2 bands (single most severe band, against the patient's baseline in ICU) and trend
    n = snap.news2
    if n is not None:
        base = _baseline(state, snap.ts) if th.news2_mode == "baseline" else None
        step = _news2_findings(n, th, base, exertion)
        if th.rising_alerts and base is not None and not step and not exertion \
                and n.total - base.total >= BASELINE_RISE:
            step = [Finding("NEWS2_RISING", "medium", "news2.rising_vs_baseline",
                            f"NEWS2 {n.total} is {n.total - base.total:.0f} above this patient's 4-h baseline "
                            f"({base.total:.0f})", True, {"from": base.total, "to": n.total})]
        guard = [f for f in step if f.rule_id == "news2.catastrophic"]
        step = [f for f in step if f.rule_id != "news2.catastrophic"]
        held = _sustained("news2_step", bool(step), not step, snap, state, th.news2_persist_s)
        out += guard + (step if held is not None else [])

        cutoff = snap.ts - NEWS2_TREND_WINDOW
        hist = [(t, s) for t, s in state.news2_history if datetime.fromisoformat(t) >= cutoff]
        if not any(f.type == "NEWS2_RISING" for f in out) and hist \
                and n.total - min(s for _, s in hist) >= NEWS2_TREND_DELTA:
            base_min = min(s for _, s in hist)
            out.append(Finding("NEWS2_RISING", "low", "news2.rising",
                               f"NEWS2 rose from {base_min} to {n.total} within 60 min", False,
                               {"from": base_min, "to": n.total}))
        hist.append((snap.ts.isoformat(), n.total))
        state.news2_history = hist[-240:]
        if th.news2_mode == "baseline" and not exertion:  # the baseline describes the patient at rest
            minute = snap.ts.replace(second=0, microsecond=0).isoformat()
            keep = [e for e in state.baseline if datetime.fromisoformat(e[0]) >= snap.ts - BASELINE_WINDOW
                    and e[0] != minute]
            state.baseline = [*keep, (minute, n.total, list(n.red_params))]

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
        high = th.hr_high + HR_HIGH_ACTIVITY_ALLOWANCE.get(snap.activity or "", 0)
        low = th.hr_low - (HR_LOW_SLEEP_ALLOWANCE if snap.activity == "sleep" else 0)
        ctx = f" during {snap.activity}" if snap.activity in (*HR_HIGH_ACTIVITY_ALLOWANCE, "sleep") else ""
        held = _sustained("hr_high", hr > high, hr < high - th.hr_hysteresis, snap, state, th.sustain_s)
        if held is not None:
            out.append(Finding("HR_SUSTAINED_HIGH", "medium", "hr.sustained_high",
                               f"HR {hr:.0f}/min above {high:.0f}{ctx} for {held / 60:.1f} min", True,
                               {"heart_rate": hr, "threshold": high, "sustained_s": held, "activity": snap.activity}))
        held = _sustained("hr_low", hr < low, hr > low + th.hr_hysteresis, snap, state, th.sustain_s)
        if held is not None:
            out.append(Finding("HR_SUSTAINED_LOW", "medium", "hr.sustained_low",
                               f"HR {hr:.0f}/min below {low:.0f}{ctx} for {held / 60:.1f} min", True,
                               {"heart_rate": hr, "threshold": low, "sustained_s": held, "activity": snap.activity}))

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
