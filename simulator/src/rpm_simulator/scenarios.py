"""Scripted deterioration scenarios injected into a patient's stream (live from CLI / console / control topic).

Each scenario ramps metric offsets in over `ramp_s`, holds them for the rest of `duration_s`, then recovers
over `recover_s`. Special behaviours (device offline, motion artefact, discrete events) are flags.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    description: str
    deltas: dict[str, float] = field(default_factory=dict)  # metric -> offset at full effect
    ramp_s: float = 300
    default_duration_s: float = 900
    recover_s: float = 180
    offline: bool = False
    motion_artifact: bool = False
    arrhythmia: bool = False
    emit_event: str | None = None  # SOS | FALL, emitted once at start


SCENARIOS: dict[str, ScenarioSpec] = {s.name: s for s in [
    ScenarioSpec("sepsis", "Early sepsis: tachycardia, fever, tachypnoea, falling SBP and SpO2",
                 {"heart_rate": 45, "temp": 2.1, "skin_temp": 1.2, "resp_rate": 10, "sbp": -32, "dbp": -18,
                  "spo2": -5, "hrv_rmssd": -12}, ramp_s=420),
    ScenarioSpec("chf_decompensation", "Heart-failure decompensation: rising HR, desaturation, tachypnoea",
                 {"heart_rate": 28, "spo2": -7, "resp_rate": 9, "sbp": 12, "hrv_rmssd": -10, "steps": -1},
                 ramp_s=480),
    ScenarioSpec("copd_exacerbation", "COPD exacerbation: marked desaturation and tachypnoea",
                 {"spo2": -9, "resp_rate": 11, "heart_rate": 22, "hrv_rmssd": -8}, ramp_s=360),
    ScenarioSpec("hypoxia", "Isolated desaturation", {"spo2": -12}, ramp_s=120, default_duration_s=600),
    ScenarioSpec("af_rvr", "Atrial fibrillation with rapid ventricular response",
                 {"heart_rate": 62, "hrv_rmssd": 45}, ramp_s=20, default_duration_s=600, arrhythmia=True),
    ScenarioSpec("bradycardia", "Symptomatic bradycardia", {"heart_rate": -36}, ramp_s=60, default_duration_s=600),
    ScenarioSpec("motion_artifact", "Vigorous movement corrupting PPG (must NOT alert)", {}, ramp_s=1,
                 default_duration_s=300, recover_s=1, motion_artifact=True),
    ScenarioSpec("device_offline", "Connectivity loss; data buffered and backfilled on reconnect", {}, ramp_s=1,
                 default_duration_s=300, recover_s=1, offline=True),
    ScenarioSpec("fall", "Fall detected by the watch", {}, ramp_s=1, default_duration_s=5, recover_s=1,
                 emit_event="FALL"),
    ScenarioSpec("sos", "Patient presses SOS", {}, ramp_s=1, default_duration_s=5, recover_s=1, emit_event="SOS"),
]}


@dataclass
class ActiveScenario:
    spec: ScenarioSpec
    started: float  # monotonic seconds
    duration_s: float
    event_emitted: bool = False
    ramp_override_s: float | None = None  # presenters compress a deterioration for live demos

    @property
    def ramp_s(self) -> float:
        return self.ramp_override_s or self.spec.ramp_s

    def intensity(self, now: float) -> float:
        """0..1 effect multiplier; 0 once fully recovered."""
        t = now - self.started
        if t < 0:
            return 0.0
        if t < self.ramp_s:
            return t / self.ramp_s
        if t < self.duration_s:
            return 1.0
        rec = t - self.duration_s
        return max(0.0, 1.0 - rec / self.spec.recover_s)

    def finished(self, now: float) -> bool:
        return now - self.started >= self.duration_s + self.spec.recover_s

    def in_hold(self, now: float) -> bool:
        return now - self.started < self.duration_s
