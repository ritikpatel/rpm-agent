"""National Early Warning Score 2 (Royal College of Physicians, 2017).

Pure functions, no I/O, so the same code runs in the stream processor, the watch app and the data team's
notebooks. Post-discharge wearables do not measure every parameter, so the score reports which parameters
were present (`partial=True` when any are missing) instead of silently treating them as normal.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

PARAMS = ("resp_rate", "spo2", "air_or_oxygen", "sbp", "heart_rate", "consciousness", "temp")

BAND_LOW = "low"
BAND_LOW_MEDIUM = "low-medium"  # a single parameter scored 3: urgent ward-based response
BAND_MEDIUM = "medium"  # 5-6: urgent response
BAND_HIGH = "high"  # >= 7: emergency response


def _round_half_up(x: float, ndigits: int = 0) -> float:
    q = 10**ndigits
    return math.floor(x * q + 0.5) / q


def score_resp_rate(rr: float) -> int:
    rr = _round_half_up(rr)
    if rr <= 8:
        return 3
    if rr <= 11:
        return 1
    if rr <= 20:
        return 0
    if rr <= 24:
        return 2
    return 3


def score_spo2_scale1(spo2: float) -> int:
    spo2 = _round_half_up(spo2)
    if spo2 <= 91:
        return 3
    if spo2 <= 93:
        return 2
    if spo2 <= 95:
        return 1
    return 0


def score_spo2_scale2(spo2: float, on_oxygen: bool) -> int:
    """Scale 2 is for patients with confirmed hypercapnic respiratory failure (target 88-92 %)."""
    spo2 = _round_half_up(spo2)
    if spo2 <= 83:
        return 3
    if spo2 <= 85:
        return 2
    if spo2 <= 87:
        return 1
    if spo2 <= 92 or not on_oxygen:
        return 0
    if spo2 <= 94:
        return 1
    if spo2 <= 96:
        return 2
    return 3


def score_air_or_oxygen(on_oxygen: bool) -> int:
    return 2 if on_oxygen else 0


def score_sbp(sbp: float) -> int:
    sbp = _round_half_up(sbp)
    if sbp <= 90:
        return 3
    if sbp <= 100:
        return 2
    if sbp <= 110:
        return 1
    if sbp <= 219:
        return 0
    return 3


def score_heart_rate(hr: float) -> int:
    hr = _round_half_up(hr)
    if hr <= 40:
        return 3
    if hr <= 50:
        return 1
    if hr <= 90:
        return 0
    if hr <= 110:
        return 1
    if hr <= 130:
        return 2
    return 3


def score_consciousness(acvpu: str) -> int:
    """A = alert (0). New Confusion, Voice, Pain, Unresponsive all score 3."""
    return 0 if acvpu.upper() == "A" else 3


def score_temp(temp_c: float) -> int:
    t = _round_half_up(temp_c, 1)
    if t <= 35.0:
        return 3
    if t <= 36.0:
        return 1
    if t <= 38.0:
        return 0
    if t <= 39.0:
        return 1
    return 2


def gcs_to_acvpu(gcs_total: float) -> str:
    """Approximate GCS -> ACVPU mapping (documented limitation; any non-'A' value scores 3 anyway)."""
    g = int(_round_half_up(gcs_total))
    if g >= 15:
        return "A"
    if g == 14:
        return "C"
    if g >= 9:
        return "V"
    if g >= 4:
        return "P"
    return "U"


def consciousness_from_gcs(gcs_total: float, baseline_best: float | None = None) -> str:
    """NEWS2 scores *new* confusion. Against the patient's own best GCS in this episode, a drop of >= 2 points
    (or an absolute GCS <= 12) is treated as new alteration; a stable, chronically reduced GCS stays 'A' so it
    does not re-alert every window. Without a baseline, falls back to the absolute mapping."""
    g = _round_half_up(gcs_total)
    if baseline_best is None:
        return gcs_to_acvpu(g)
    if g >= 15 or (g >= 13 and baseline_best - g < 2):
        return "A"
    return "C" if g >= 13 else gcs_to_acvpu(g)


@dataclass(frozen=True)
class News2Input:
    resp_rate: float | None = None
    spo2: float | None = None
    on_oxygen: bool | None = None
    sbp: float | None = None
    heart_rate: float | None = None
    consciousness: str | None = None  # A / C / V / P / U
    temp: float | None = None
    hypercapnic_resp_failure: bool = False  # selects SpO2 scale 2


@dataclass(frozen=True)
class News2Result:
    total: int
    band: str
    partial: bool
    spo2_scale: int
    params: dict[str, int] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    red_params: tuple[str, ...] = ()
    assumed: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        d = asdict(self)
        d["missing"] = list(self.missing)
        d["red_params"] = list(self.red_params)
        d["assumed"] = list(self.assumed)
        return d

    def explain(self) -> str:
        parts = ", ".join(f"{k} {v}" for k, v in self.params.items() if v > 0) or "all parameters normal"
        suffix = f"; missing {', '.join(self.missing)}" if self.missing else ""
        return f"NEWS2 {self.total} ({self.band}): {parts}{suffix}"


def band_for(total: int, red_params: tuple[str, ...]) -> str:
    if total >= 7:
        return BAND_HIGH
    if total >= 5:
        return BAND_MEDIUM
    if red_params:
        return BAND_LOW_MEDIUM
    return BAND_LOW


def compute_news2(v: News2Input) -> News2Result:
    params: dict[str, int] = {}
    missing: list[str] = []
    assumed: list[str] = []

    on_oxygen = v.on_oxygen
    if on_oxygen is None:
        on_oxygen = False
        assumed.append("air_or_oxygen=air")

    if v.resp_rate is not None:
        params["resp_rate"] = score_resp_rate(v.resp_rate)
    else:
        missing.append("resp_rate")

    scale = 2 if v.hypercapnic_resp_failure else 1
    if v.spo2 is not None:
        params["spo2"] = score_spo2_scale2(v.spo2, on_oxygen) if scale == 2 else score_spo2_scale1(v.spo2)
    else:
        missing.append("spo2")

    params["air_or_oxygen"] = score_air_or_oxygen(on_oxygen)

    if v.sbp is not None:
        params["sbp"] = score_sbp(v.sbp)
    else:
        missing.append("sbp")

    if v.heart_rate is not None:
        params["heart_rate"] = score_heart_rate(v.heart_rate)
    else:
        missing.append("heart_rate")

    if v.consciousness is not None:
        params["consciousness"] = score_consciousness(v.consciousness)
    else:
        missing.append("consciousness")

    if v.temp is not None:
        params["temp"] = score_temp(v.temp)
    else:
        missing.append("temp")

    total = sum(params.values())
    red = tuple(k for k, s in params.items() if s == 3)
    return News2Result(
        total=total,
        band=band_for(total, red),
        partial=bool(missing),
        spo2_scale=scale,
        params=params,
        missing=tuple(missing),
        red_params=red,
        assumed=tuple(assumed),
    )
