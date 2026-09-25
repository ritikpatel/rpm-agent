"""Minimal HL7 v2 codec for bedside-monitor results: ORU^R01 in, ACK out, MLLP framing.

Only what an ICU monitor feed needs (MSH, PID, OBR, OBX; numeric observations). Deliberately dependency-free so it
runs unchanged inside a Greengrass component. Field separator and encoding characters are read from MSH, not assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone

VT, FS, CR = b"\x0b", b"\x1c", b"\x0d"
MAX_MESSAGE = 64 * 1024  # a monitor ORU is ~1 KB; anything this large is a broken or hostile peer


class Hl7Error(ValueError):
    pass


# ---------------------------------------------------------------- MLLP framing
def frame(message: str) -> bytes:
    return VT + message.encode("utf-8") + FS + CR


class MllpDecoder:
    """Incremental MLLP de-framer: feed() raw socket bytes, get back complete messages. Bytes outside a
    <VT>...<FS><CR> block are discarded (resynchronises after a peer glitch)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[str]:
        self._buf += data
        out: list[str] = []
        while True:
            start = self._buf.find(VT)
            if start < 0:
                self._buf.clear()
                return out
            end = self._buf.find(FS + CR, start)
            if end < 0:
                if len(self._buf) - start > MAX_MESSAGE:
                    raise Hl7Error("MLLP message exceeds size limit")
                del self._buf[:start]
                return out
            out.append(self._buf[start + 1:end].decode("utf-8", errors="replace"))
            del self._buf[:end + 2]


# ---------------------------------------------------------------- parsing
@dataclass(frozen=True)
class Observation:
    set_id: str
    value_type: str
    code: str            # OBX-3.1 identifier, e.g. 147842
    code_text: str       # OBX-3.2, e.g. MDC_ECG_HEART_RATE
    system: str          # OBX-3.3, e.g. MDC or LN
    value: str
    unit: str            # OBX-6.1 (code) or .2 (text), whichever is present
    status: str          # OBX-11: F final, C corrected, X cannot be obtained, P preliminary...
    observed_at: datetime | None
    equipment: str       # OBX-18: equipment instance identifier


@dataclass
class Message:
    control_id: str
    message_type: str    # e.g. ORU^R01
    sending_app: str
    sending_facility: str
    version: str
    sent_at: datetime | None
    observations: list[Observation] = field(default_factory=list)
    had_pid: bool = False

    @property
    def device(self) -> str:
        """Bedside device identity: MSH-4 sending facility (the monitor), falling back to OBX-18."""
        return self.sending_facility or next((o.equipment for o in self.observations if o.equipment), "")


def parse_ts(v: str) -> datetime | None:
    """HL7 DTM (YYYYMMDDHHMM[SS[.S+]][+/-ZZZZ]). Timestamps without an offset are taken as UTC, the convention the
    gateway requires monitors to be configured with."""
    v = v.strip()
    if not v:
        return None
    tz = UTC
    for sign in ("+", "-"):
        if sign in v[8:]:
            i = v.rindex(sign)
            off = v[i + 1:]
            hours, minutes = int(off[:2]), int(off[2:4] or 0)
            tz = timezone((1 if sign == "+" else -1) * timedelta(hours=hours, minutes=minutes))
            v = v[:i]
            break
    frac = 0
    if "." in v:
        v, f = v.split(".", 1)
        frac = int((f + "000000")[:6])
    fmt = {8: "%Y%m%d", 12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}.get(len(v))
    if fmt is None:
        raise Hl7Error(f"bad HL7 timestamp {v!r}")
    return datetime.strptime(v, fmt).replace(microsecond=frac, tzinfo=tz).astimezone(UTC)


def parse(message: str) -> Message:
    segments = [s for s in message.replace("\r\n", "\r").replace("\n", "\r").split("\r") if s]
    if not segments or not segments[0].startswith("MSH") or len(segments[0]) < 8:
        raise Hl7Error("message does not start with MSH")
    fs = segments[0][3]
    comp = segments[0][4]

    def fields(seg: str) -> list[str]:
        f = seg.split(fs)
        # MSH-1 is the separator itself, so MSH field n sits at index n-1; other segments at index n
        return [f[0], fs, *f[1:]] if f[0] == "MSH" else f

    def get(f: list[str], i: int) -> str:
        return f[i] if i < len(f) else ""

    def comps(v: str) -> list[str]:
        return v.split(comp)

    msh = fields(segments[0])
    msg = Message(control_id=get(msh, 10), message_type=get(msh, 9).replace(comp, "^"),
                  sending_app=comps(get(msh, 3))[0], sending_facility=comps(get(msh, 4))[0],
                  version=get(msh, 12), sent_at=parse_ts(get(msh, 7)))
    for seg in segments[1:]:
        f = fields(seg)
        if f[0] == "PID":
            msg.had_pid = True
        elif f[0] == "OBX":
            ident = comps(get(f, 3)) + ["", "", ""]
            unit = comps(get(f, 6)) + [""]
            msg.observations.append(Observation(
                set_id=get(f, 1), value_type=get(f, 2), code=ident[0], code_text=ident[1], system=ident[2],
                value=get(f, 5), unit=unit[0] or unit[1], status=get(f, 11) or "F",
                observed_at=parse_ts(get(f, 14)), equipment=comps(get(f, 18))[0]))
    return msg


# ---------------------------------------------------------------- building
def ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%d%H%M%S")


def ack(msg: Message | None, code: str, text: str = "", *, app: str = "RPM-GW", facility: str = "") -> str:
    """AA accept · AE error (resend won't help) · AR reject (unknown device, wrong message type)."""
    control = msg.control_id if msg else ""
    now = ts(datetime.now(UTC))
    to_app, to_fac = (msg.sending_app, msg.sending_facility) if msg else ("", "")
    return "\r".join([
        f"MSH|^~\\&|{app}|{facility}|{to_app}|{to_fac}|{now}||ACK^R01^ACK|ACK{control}|P|2.6",
        f"MSA|{code}|{control}|{text[:80]}",
    ]) + "\r"


def ack_code(message: str) -> str:
    """MSA-1 of an ACK: AA, AE or AR ('' when there is no MSA segment)."""
    for seg in message.replace("\n", "\r").split("\r"):
        if seg.startswith("MSA") and len(seg) > 4:
            return seg.split(seg[3])[1]
    return ""


def oru(device: str, control_id: str, observed_at: datetime, obs: list[tuple[str, str, str, float | str, str]],
        *, patient_ref: str | None = None) -> str:
    """ORU^R01 as a monitor sends it. obs = [(code, text, system, value, unit)]."""
    t = ts(observed_at)
    segs = [f"MSH|^~\\&|MONITOR|{device}|RPM-GW|ICU|{t}||ORU^R01^ORU_R01|{control_id}|P|2.6|||AL|NE"]
    if patient_ref:
        segs.append(f"PID|||{patient_ref}^^^HOSP^MR")
    segs.append(f"OBR|1|||69837^MDC_DEV_METER_PHYSIO_MULTI_PARAM_MDS^MDC|||{t}")
    for i, (code, text, system, value, unit) in enumerate(obs, 1):
        segs.append(f"OBX|{i}|NM|{code}^{text}^{system}||{value}|{unit}|||||F|||{t}||||{device}")
    return "\r".join(segs) + "\r"
