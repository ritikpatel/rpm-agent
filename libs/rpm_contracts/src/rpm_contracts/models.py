"""Pydantic mirror of /contracts/schemas. Keep in lock-step with the JSON Schemas (tests enforce it)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ids import uuid7

Source = Literal["galaxy_watch", "wearos_hs", "wearos_emulator", "simulator", "icu_gateway"]
Context = Literal["home", "icu"]
Profile = Literal["normal", "elevated", "critical"]
Severity = Literal["info", "low", "medium", "high", "critical"]
SEVERITY_RANK: dict[str, int] = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

DEVICE_ID_PATTERN = r"^[A-Za-z0-9_-]{3,64}$"


def utcnow() -> datetime:
    return datetime.now(UTC)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))


# --------------------------------------------------------------------------- telemetry
QualityT = Annotated[float | None, Field(ge=0, le=1)]


class Scalar(_Model):
    value: float
    quality: QualityT = None


class SpO2(_Model):
    value: float = Field(ge=50, le=100)
    quality: QualityT = None
    spot_check: bool | None = None


class Aggregate(_Model):
    mean: float
    min: float | None = None
    max: float | None = None
    n: int | None = Field(default=None, ge=0)
    quality: QualityT = None


class Activity(_Model):
    value: Literal["resting", "light", "moderate", "vigorous", "sleep", "unknown"]


class Metrics(_Model):
    heart_rate: Aggregate | None = None
    hrv_rmssd: Scalar | None = None
    spo2: SpO2 | None = None
    resp_rate: Scalar | None = None
    sbp: Scalar | None = None
    dbp: Scalar | None = None
    temp: Scalar | None = None
    skin_temp: Scalar | None = None
    gcs: Scalar | None = None
    steps: Scalar | None = None
    activity: Activity | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> Metrics:
        if not any(getattr(self, f) is not None for f in type(self).model_fields):
            raise ValueError("metrics must contain at least one measurement")
        return self


class TelemetryTs(_Model):
    window_start: datetime
    window_end: datetime
    edge_sent: datetime


class EdgeInfo(_Model):
    flags: list[str] = Field(default_factory=list)
    profile: Profile | None = None
    buffered: bool = False


class DeviceInfo(_Model):
    battery_pct: float | None = Field(default=None, ge=0, le=100)
    on_wrist: bool | None = None
    fw: str | None = None


class Telemetry(_Model):
    schema_: Literal["rpm.telemetry.v1"] = Field(default="rpm.telemetry.v1", alias="schema")
    msg_id: UUID = Field(default_factory=uuid7)
    device_id: str = Field(pattern=DEVICE_ID_PATTERN)
    seq: int = Field(ge=0)
    source: Source
    context: Context = "home"
    ts: TelemetryTs
    metrics: Metrics
    edge: EdgeInfo | None = None
    device: DeviceInfo | None = None


# --------------------------------------------------------------------------- device events
DeviceEventType = Literal["SOS", "FALL", "EDGE_ALERT", "CHECKIN_REPLY", "DEVICE_STATUS", "SPOT_CHECK_RESULT"]


class DeviceEventTs(_Model):
    occurred: datetime
    edge_sent: datetime


class DeviceEvent(_Model):
    schema_: Literal["rpm.device_event.v1"] = Field(default="rpm.device_event.v1", alias="schema")
    msg_id: UUID = Field(default_factory=uuid7)
    device_id: str = Field(pattern=DEVICE_ID_PATTERN)
    seq: int = Field(ge=0)
    source: Source
    type: DeviceEventType
    ts: DeviceEventTs
    data: dict[str, Any] | None = None


# --------------------------------------------------------------------------- commands & shadow
CommandType = Literal["SET_PROFILE", "SPOT_CHECK", "CHECKIN", "NOTIFY", "INCOMING_CALL"]


class Command(_Model):
    schema_: Literal["rpm.command.v1"] = Field(default="rpm.command.v1", alias="schema")
    cmd_id: UUID = Field(default_factory=uuid7)
    cmd: CommandType
    issued_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None
    issued_by: str | None = None
    args: dict[str, Any] | None = None

    def expired(self, now: datetime | None = None) -> bool:
        return self.expires_at is not None and (now or utcnow()) > self.expires_at


class Thresholds(_Model):
    hr_high: float | None = None
    hr_low: float | None = None
    spo2_low: float | None = None
    sustain_s: int | None = Field(default=None, ge=0)


class Shadow(_Model):
    profile: Profile | None = None
    summary_window_s: Literal[5, 15, 30, 60] | None = None
    spo2_spot_interval_min: int | None = Field(default=None, ge=5, le=240)
    thresholds: Thresholds | None = None


PROFILE_WINDOW_S: dict[str, int] = {"normal": 30, "elevated": 15, "critical": 5}


class BedConfig(_Model):
    spo2_scale: Literal[1, 2] | None = None
    on_supplemental_o2: bool | None = None
    gcs_baseline: float | None = Field(default=None, ge=3, le=15)


class GatewayConfig(_Model):
    """Retained per-bed clinical configuration for an ICU gateway (rpm/v1/dev/{gateway}/config)."""

    schema_: Literal["rpm.gateway_config.v1"] = Field(default="rpm.gateway_config.v1", alias="schema")
    gateway_id: str = Field(pattern=DEVICE_ID_PATTERN)
    issued_at: datetime = Field(default_factory=utcnow)
    beds: dict[str, BedConfig]


# --------------------------------------------------------------------------- clinical events
ClinicalEventType = Literal[
    "NEWS2_HIGH", "NEWS2_MEDIUM", "NEWS2_RED_PARAM", "NEWS2_RISING",
    "HR_SUSTAINED_HIGH", "HR_SUSTAINED_LOW", "SPO2_LOW", "SPO2_CRITICAL",
    "SOS", "FALL", "EDGE_ALERT", "DEVICE_SILENT", "DATA_GAP", "DEVICE_STATUS", "CHECKIN_REPLY",
]


class ClinicalEvent(_Model):
    schema_: Literal["rpm.clinical_event.v1"] = Field(default="rpm.clinical_event.v1", alias="schema")
    event_id: UUID = Field(default_factory=uuid7)
    patient_id: str
    device_id: str | None = None
    context: Context | None = None
    type: ClinicalEventType
    severity: Severity
    occurred_at: datetime
    detected_at: datetime = Field(default_factory=utcnow)
    rule_id: str
    summary: str
    requires_alert: bool
    evidence: dict[str, Any] = Field(default_factory=dict)
    news2: dict[str, Any] | None = None
    trace: dict[str, Any] | None = None


# --------------------------------------------------------------------------- ingest envelope
class IngestMeta(_Model):
    """Fields the IoT rule (or local emulator) adds on top of the device payload."""

    thing: str
    kind: Literal["telemetry", "events"]
    ingest_ts_ms: int

    @property
    def ingest_at(self) -> datetime:
        return datetime.fromtimestamp(self.ingest_ts_ms / 1000, UTC)


INGEST_FIELDS = ("thing", "kind", "ingest_ts_ms")


def split_ingest_record(record: dict[str, Any]) -> tuple[IngestMeta, dict[str, Any]]:
    """Separate IoT-rule enrichment from the original device payload."""
    payload = dict(record)
    meta = IngestMeta(**{k: payload.pop(k) for k in INGEST_FIELDS})
    return meta, payload


def telemetry_topic(device_id: str, kind: str) -> str:
    return f"rpm/v1/dev/{device_id}/{kind}"
