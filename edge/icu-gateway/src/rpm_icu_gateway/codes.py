"""Observation code -> canonical metric. Bedside monitors report IEEE 11073-10101 (MDC) codes; interface engines
often rewrite them to LOINC. Both are accepted. Units are normalised to what rpm.telemetry.v1 expects."""

from __future__ import annotations

from .hl7 import Observation

# metric: ({MDC codes}, {LOINC codes})
CODES: dict[str, tuple[set[str], set[str]]] = {
    "heart_rate": ({"147842", "149530", "16770"}, {"8867-4"}),   # ECG HR, SpO2 pulse rate, generic cardiac rate
    "spo2": ({"150456"}, {"59408-5", "2708-6"}),
    "resp_rate": ({"151562", "151554"}, {"9279-1"}),              # RR, impedance RR
    "sbp": ({"150021", "150017", "150033"}, {"8480-6"}),          # NIBP sys, generic sys, arterial sys
    "dbp": ({"150022", "150018", "150034"}, {"8462-4"}),
    "temp": ({"150364", "150344", "188496"}, {"8310-5", "8331-1"}),
    "gcs": (set(), {"9269-2"}),                                    # nurse-charted, forwarded by the monitor
}
_BY_CODE = {(c, sys): metric for metric, (mdc, loinc) in CODES.items()
            for sys, codes in (("MDC", mdc), ("LN", loinc)) for c in codes}
TEXT_ALIASES = {"MDC_ECG_HEART_RATE": "heart_rate", "MDC_PULS_OXIM_SAT_O2": "spo2", "MDC_RESP_RATE": "resp_rate",
                "MDC_PRESS_BLD_NONINV_SYS": "sbp", "MDC_PRESS_BLD_NONINV_DIA": "dbp", "MDC_TEMP_BODY": "temp"}

# Plausibility limits: values outside are sensor or transcription faults, not physiology.
LIMITS = {"heart_rate": (15, 300), "spo2": (50, 100), "resp_rate": (2, 80), "sbp": (40, 300), "dbp": (15, 200),
          "temp": (25, 45), "gcs": (3, 15)}

# What a monitor feed carries, for the simulator side: metric -> (code, text, system, unit)
MONITOR_CODES = {
    "heart_rate": ("147842", "MDC_ECG_HEART_RATE", "MDC", "264864^MDC_DIM_BEAT_PER_MIN^MDC"),
    "spo2": ("150456", "MDC_PULS_OXIM_SAT_O2", "MDC", "262688^MDC_DIM_PERCENT^MDC"),
    "resp_rate": ("151562", "MDC_RESP_RATE", "MDC", "264928^MDC_DIM_RESP_PER_MIN^MDC"),
    "sbp": ("150021", "MDC_PRESS_BLD_NONINV_SYS", "MDC", "266016^MDC_DIM_MMHG^MDC"),
    "dbp": ("150022", "MDC_PRESS_BLD_NONINV_DIA", "MDC", "266016^MDC_DIM_MMHG^MDC"),
    "temp": ("150364", "MDC_TEMP_BODY", "MDC", "268192^MDC_DIM_DEGC^MDC"),
    "gcs": ("9269-2", "Glasgow coma score total", "LN", "{score}"),
}


def metric_of(o: Observation) -> str | None:
    system = {"MDC": "MDC", "LN": "LN", "LOINC": "LN"}.get(o.system.upper(), o.system.upper())
    return _BY_CODE.get((o.code, system)) or TEXT_ALIASES.get(o.code_text.upper())


def value_of(o: Observation, metric: str) -> float | None:
    """Numeric value in canonical units, or None if the observation is not usable (status X/D, non-numeric,
    implausible)."""
    if o.status.upper() in ("X", "D", "W") or o.value_type not in ("NM", "SN", ""):
        return None
    try:
        v = float(o.value.split("^")[-1] if o.value_type == "SN" else o.value)
    except ValueError:
        return None
    unit = o.unit.upper()
    if metric == "temp" and ("FAH" in unit or unit in ("[DEGF]", "DEGF", "266560")):
        v = (v - 32) * 5 / 9
    lo, hi = LIMITS[metric]
    return v if lo <= v <= hi else None
