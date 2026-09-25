"""Signal generators: post-discharge wearable physiology and ICU replay of charted MIMIC vitals."""

from __future__ import annotations

import bisect
import math
import random
from dataclasses import dataclass


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


@dataclass
class Window:
    """Everything a device would report for one summary window."""

    metrics: dict[str, dict]
    quality_ok: bool = True


class HomeWearable:
    """Galaxy-Watch-like stream: HR (+HRV), spot SpO2, wrist skin temperature, steps/activity.

    Patient-entered home readings (BP cuff, thermometer) are merged in periodically, as the phone gateway would.
    """

    def __init__(self, baseline: dict[str, float], rng: random.Random, spo2_every_s: float = 300,
                 manual_every_s: float = 1200):
        self.b = baseline
        self.rng = rng
        self.spo2_every_s = spo2_every_s
        self.manual_every_s = manual_every_s
        self._last_spo2 = -1e9
        self._last_manual = -1e9
        self._activity = "resting"

    def _activity_for(self, hour: float) -> str:
        if hour < 6 or hour >= 23:
            return "sleep"
        if self.rng.random() < 0.08:  # occasionally change state
            self._activity = self.rng.choices(["resting", "light", "moderate"], [0.6, 0.32, 0.08])[0]
        return self._activity

    def sample(self, t: float, hour_of_day: float, window_s: float, deltas: dict[str, float],
               motion_artifact: bool, arrhythmia: bool, force_spo2: bool = False) -> Window:
        r = self.rng
        act = self._activity_for(hour_of_day)
        circadian = -6 * math.cos((hour_of_day - 4) / 24 * 2 * math.pi)  # nadir ~04:00
        act_hr = {"sleep": -8, "resting": 0, "light": 12, "moderate": 25}[act]
        hr = self.b["heart_rate"] + circadian + act_hr + deltas.get("heart_rate", 0) + r.gauss(0, 2.0)
        spread = 14 if arrhythmia else 5
        hr_q = _clamp(r.gauss(0.92, 0.04), 0.7, 1.0)
        if motion_artifact:
            hr = r.uniform(150, 180)
            hr_q = r.uniform(0.15, 0.4)
            act = "vigorous"
        hr = _clamp(hr, 30, 210)
        hrv = _clamp(35 - 0.25 * (hr - 70) + deltas.get("hrv_rmssd", 0) + r.gauss(0, 3), 4, 120)
        steps = {"sleep": 0, "resting": r.randint(0, 6), "light": r.randint(20, 45),
                 "moderate": r.randint(45, 70), "vigorous": r.randint(60, 90)}[act] * window_s / 30
        if deltas.get("steps", 0) < 0:
            steps *= 0.3

        m: dict[str, dict] = {
            "heart_rate": {"mean": round(hr, 1), "min": round(hr - r.uniform(2, spread), 1),
                           "max": round(hr + r.uniform(2, spread), 1), "n": int(window_s), "quality": round(hr_q, 2)},
            "hrv_rmssd": {"value": round(hrv, 1), "quality": round(_clamp(hr_q - 0.05, 0, 1), 2)},
            "skin_temp": {"value": round(33.4 + deltas.get("skin_temp", 0) + r.gauss(0, 0.15)
                                         - (0.6 if act == "sleep" else 0), 2)},
            "steps": {"value": float(int(steps))},
            "activity": {"value": act},
        }
        spo2_due = force_spo2 or t - self._last_spo2 >= self.spo2_every_s
        if spo2_due and not motion_artifact:
            self._last_spo2 = t
            spo2 = _clamp(self.b["spo2"] + deltas.get("spo2", 0) + r.gauss(0, 0.6), 70, 100)
            m["spo2"] = {"value": round(spo2, 1), "quality": round(_clamp(r.gauss(0.9, 0.05), 0.6, 1), 2),
                         "spot_check": True}
        if t - self._last_manual >= self.manual_every_s:
            self._last_manual = t
            m["sbp"] = {"value": round(self.b["sbp"] + deltas.get("sbp", 0) + r.gauss(0, 4))}
            m["dbp"] = {"value": round(self.b["dbp"] + deltas.get("dbp", 0) + r.gauss(0, 3))}
            m["temp"] = {"value": round(self.b["temp"] + deltas.get("temp", 0) + r.gauss(0, 0.1), 1)}
            m["resp_rate"] = {"value": round(self.b["resp_rate"] + deltas.get("resp_rate", 0) + r.gauss(0, 1))}
        return Window(m, quality_ok=not motion_artifact)


class IcuReplay:
    """Replays a charted ICU stay at `speed`x, linearly interpolating between (sparse) charted points and
    adding monitor-like noise. Loops when the stay ends so the demo can run indefinitely."""

    NOISE = {"heart_rate": 1.5, "spo2": 0.4, "resp_rate": 0.8, "sbp": 2.5, "dbp": 2.0, "temp": 0.04}

    def __init__(self, series: dict[str, list[tuple[float, float]]], rng: random.Random, start_h: float,
                 speed: float):
        self.series = {k: v for k, v in series.items() if v}
        self.rng = rng
        self.start_h = start_h
        self.speed = speed
        self.length_h = max((v[-1][0] for v in self.series.values()), default=24.0) or 24.0

    def _at(self, key: str, h: float) -> float | None:
        pts = self.series.get(key)
        if not pts:
            return None
        xs = [p[0] for p in pts]
        i = bisect.bisect_right(xs, h)
        if i == 0:
            return pts[0][1]
        if i == len(pts) or key == "gcs":  # GCS is a step function
            return pts[i - 1][1]
        (x0, y0), (x1, y1) = pts[i - 1], pts[i]
        return y0 + (y1 - y0) * (h - x0) / (x1 - x0) if x1 > x0 else y1

    def stay_hour(self, elapsed_real_s: float) -> float:
        return (self.start_h + elapsed_real_s * self.speed / 3600) % max(self.length_h, 1.0)

    def sample(self, elapsed_real_s: float, deltas: dict[str, float], arrhythmia: bool) -> Window:
        h = self.stay_hour(elapsed_real_s)
        r = self.rng
        m: dict[str, dict] = {}
        for key in ("heart_rate", "spo2", "resp_rate", "sbp", "dbp", "temp", "gcs"):
            v = self._at(key, h)
            if v is None:
                continue
            v += deltas.get(key, 0) + r.gauss(0, self.NOISE.get(key, 0))
            if key == "heart_rate":
                v = _clamp(v, 25, 220)
                spread = 12 if arrhythmia else 4
                m[key] = {"mean": round(v, 1), "min": round(v - r.uniform(1, spread), 1),
                          "max": round(v + r.uniform(1, spread), 1), "n": 30, "quality": 0.99}
            elif key == "spo2":
                m[key] = {"value": round(_clamp(v, 60, 100), 1), "quality": 0.98}
            elif key == "gcs":
                m[key] = {"value": float(_clamp(round(v), 3, 15))}
            elif key == "temp":
                m[key] = {"value": round(v, 1)}
            else:
                m[key] = {"value": round(v)}
        return Window(m)
