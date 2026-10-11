"""Signal generators: a wearable that carries on from the patient's last recorded reading, an ICU bedside monitor,
and (for the offline evaluation only) a wearable that plays recorded data back."""

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


class Wearable:
    """A wrist wearable: heart rate in every window, SpO2, respiratory rate and temperature every few minutes, blood
    pressure less often. Subclasses say what the patient's vital signs are at a given moment; this class turns them
    into what the device reports (sensor noise, quality, cadence). A demonstration scenario adds its offsets on top."""

    NOISE = {"heart_rate": 1.0, "spo2": 0.3, "resp_rate": 0.4, "sbp": 2.0, "temp": 0.04}

    def __init__(self, rng: random.Random, t0: float, hrv_base: float = 32.0, spo2_every_s: float = 300,
                 manual_every_s: float = 900):
        self.rng, self.t0 = rng, t0
        self.hrv_base = hrv_base
        self.spo2_every_s = spo2_every_s
        self.manual_every_s = manual_every_s     # blood pressure cadence; shortened while a scenario runs
        self._last_spo2 = -1e9
        self._last_manual = -1e9
        self._t = t0

    def values(self, t: float) -> dict[str, float]:
        raise NotImplementedError

    @property
    def b(self) -> dict[str, float]:
        """The patient's current values (what an on-demand reading would be centred on)."""
        return self.values(self._t)

    def sample(self, t: float, hour_of_day: float, window_s: float, deltas: dict[str, float],
               motion_artifact: bool, arrhythmia: bool, force_spo2: bool = False) -> Window:
        self._t = t
        r, now = self.rng, self.values(t)
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


class ContinuingWearable(Wearable):
    """Live monitoring: the patient's recorded data has ended, and new readings carry on from the last one.

    Where the record ended with the patient unwell, the values recover gradually towards the patient's own settled
    values (`well`): the gap halves every `half_life_s`. Around that course each vital sign wanders slowly and is
    pulled back, sized like the reading-to-reading variation in the recorded wearable streams (over six hours, less
    the sensor noise added on top). A patient who was settled when the record ended simply stays settled."""

    DRIFT_SD = {"heart_rate": 1.7, "spo2": 0.4, "resp_rate": 0.8, "sbp": 3.1, "temp": 0.09}
    DRIFT_TAU_S = 1200.0   # a drift fades over about 20 minutes
    SETTLED = {"heart_rate": 8.0, "spo2": 2.0, "resp_rate": 3.0, "sbp": 10.0, "temp": 0.4}   # "close enough to well"

    def __init__(self, last: dict[str, float], rng: random.Random, t0: float, well: dict[str, float] | None = None,
                 half_life_s: float = 7200.0, **kw):
        super().__init__(rng, t0, **kw)
        self.last = dict(last)
        self.well = {k: (well or {}).get(k, v) for k, v in last.items()}
        self.half_life_s = half_life_s                     # 0 = no recovery: hold at the last recorded values
        self._away = {k: last[k] - self.well[k] for k in last}   # distance from well at _away_t
        self._away_t = t0
        self._drift = dict.fromkeys(self.DRIFT_SD, 0.0)   # starts exactly on the last recorded reading
        self._drift_t = t0

    def centre(self, t: float) -> dict[str, float]:
        """Where the patient is on their way from the last recorded reading to their settled values."""
        left = 0.5 ** (max(0.0, t - self._away_t) / self.half_life_s) if self.half_life_s > 0 else 1.0
        return {k: self.well[k] + away * left for k, away in self._away.items()}

    def resume(self, values: dict[str, float], t: float) -> None:
        """Carry on from readings already reported (a restarted simulator): recovery continues from where the
        patient had got to instead of starting again from the record."""
        now = self.centre(t)
        self._away = {k: values.get(k, now[k]) - self.well[k] for k in self.well}
        self._away_t = t
        self._drift, self._drift_t = dict.fromkeys(self.DRIFT_SD, 0.0), t

    def settled(self, t: float) -> bool:
        now = self.centre(t)
        return all(abs(now[k] - self.well[k]) <= self.SETTLED.get(k, float("inf")) for k in now)

    def values(self, t: float) -> dict[str, float]:
        dt = t - self._drift_t
        if dt > 0:   # an Ornstein-Uhlenbeck step: the same statistics whatever the reporting interval
            keep = math.exp(-dt / self.DRIFT_TAU_S)
            push = math.sqrt(1 - keep * keep)
            for k, sd in self.DRIFT_SD.items():
                self._drift[k] = self._drift[k] * keep + self.rng.gauss(0, sd) * push
            self._drift_t = t
        out = {k: v + self._drift.get(k, 0.0) for k, v in self.centre(t).items()}
        out["spo2"] = min(100.0, out["spo2"])
        return out


class TrajectoryWearable(Wearable):
    """Offline evaluation only: plays a patient's recorded post-discharge data back (interpolated between its
    points), so the alert rules can be scored against the labels the data carries. The live platform never streams
    recorded readings; see ContinuingWearable."""

    def __init__(self, points: tuple, rng: random.Random, start_h: float, t0: float, speed: float = 1.0, **kw):
        super().__init__(rng, t0, **kw)
        self.points, self.start_h, self.speed = points, start_h, speed

    def hours(self, t: float) -> float:
        """Hours since discharge at simulator time t."""
        return max(0.0, self.start_h + (t - self.t0) * self.speed / 3600)

    def values(self, t: float) -> dict[str, float]:
        from rpm_core.cohort import trajectory_at

        return trajectory_at(self.points, self.hours(t))


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
