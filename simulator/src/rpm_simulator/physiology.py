"""Signal generators: a wearable replaying the patient's post-discharge data, and an ICU bedside monitor."""

from __future__ import annotations

import bisect
import random
from dataclasses import dataclass


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


@dataclass
class Window:
    """Everything a device would report for one summary window."""

    metrics: dict[str, dict]
    quality_ok: bool = True


class TrajectoryWearable:
    """A wrist wearable that reports the patient's own post-discharge course from the training data.

    The values come from the data (interpolated between its points, with a little sensor noise): heart rate in every
    window, SpO2, respiratory rate and temperature every few minutes, blood pressure less often. Nothing is invented
    beyond the noise; a demonstration scenario adds its offsets on top."""

    NOISE = {"heart_rate": 1.0, "spo2": 0.3, "resp_rate": 0.4, "sbp": 2.0, "temp": 0.04}

    def __init__(self, points: tuple, rng: random.Random, start_h: float, t0: float, speed: float = 1.0,
                 hrv_base: float = 32.0, spo2_every_s: float = 300, manual_every_s: float = 900):
        self.points, self.rng = points, rng
        self.start_h, self.t0, self.speed = start_h, t0, speed
        self.hrv_base = hrv_base
        self.spo2_every_s = spo2_every_s
        self.manual_every_s = manual_every_s     # blood pressure cadence; shortened while a scenario runs
        self._last_spo2 = -1e9
        self._last_manual = -1e9
        self._t = t0

    def hours(self, t: float) -> float:
        """Hours since discharge at simulator time t."""
        return max(0.0, self.start_h + (t - self.t0) * self.speed / 3600)

    @property
    def b(self) -> dict[str, float]:
        """The patient's current values (what an on-demand reading would be centred on)."""
        from rpm_core.cohort import trajectory_at

        return trajectory_at(self.points, self.hours(self._t))

    def sample(self, t: float, hour_of_day: float, window_s: float, deltas: dict[str, float],
               motion_artifact: bool, arrhythmia: bool, force_spo2: bool = False) -> Window:
        self._t = t
        r, now = self.rng, self.b
        act = "sleep" if (hour_of_day < 6 or hour_of_day >= 23) else "resting"
        hr = now["heart_rate"] + deltas.get("heart_rate", 0) + r.gauss(0, self.NOISE["heart_rate"])
        spread = 14 if arrhythmia else 4
        hr_q = _clamp(r.gauss(0.93, 0.03), 0.7, 1.0)
        if motion_artifact:
            hr, hr_q, act = r.uniform(150, 180), r.uniform(0.15, 0.4), "vigorous"
        hr = _clamp(hr, 30, 210)
        hrv = _clamp(self.hrv_base - 0.25 * (hr - 70) + deltas.get("hrv_rmssd", 0) + r.gauss(0, 2), 4, 120)
        m: dict[str, dict] = {
            "heart_rate": {"mean": round(hr, 1), "min": round(hr - r.uniform(1, spread), 1),
                           "max": round(hr + r.uniform(1, spread), 1), "n": int(window_s), "quality": round(hr_q, 2)},
            "hrv_rmssd": {"value": round(hrv, 1), "quality": round(_clamp(hr_q - 0.05, 0, 1), 2)},
            "activity": {"value": act},
        }
        if (force_spo2 or t - self._last_spo2 >= self.spo2_every_s) and not motion_artifact:
            self._last_spo2 = t
            spo2 = _clamp(now["spo2"] + deltas.get("spo2", 0) + r.gauss(0, self.NOISE["spo2"]), 70, 100)
            m["spo2"] = {"value": round(spo2, 1), "quality": round(_clamp(r.gauss(0.92, 0.04), 0.6, 1), 2),
                         "spot_check": True}
            m["resp_rate"] = {"value": round(_clamp(now["resp_rate"] + deltas.get("resp_rate", 0)
                                                    + r.gauss(0, self.NOISE["resp_rate"]), 4, 60), 1)}
            m["temp"] = {"value": round(now["temp"] + deltas.get("temp", 0) + r.gauss(0, self.NOISE["temp"]), 1)}
        if t - self._last_manual >= self.manual_every_s:
            self._last_manual = t
            m["sbp"] = {"value": round(now["sbp"] + deltas.get("sbp", 0) + r.gauss(0, self.NOISE["sbp"]))}
        return Window(m, quality_ok=not motion_artifact)


class IcuReplay:
    """An ICU bedside monitor: every vital sign, centred on the series it is given (for an admitted patient, the
    values charted at discharge), with monitor-like noise. A longer series is replayed at `speed`x and loops."""

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
