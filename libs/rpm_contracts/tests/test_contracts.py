"""Every example in /contracts/examples must validate against its JSON Schema AND its Pydantic model,
and Pydantic output must validate against the JSON Schema (guards drift between the two)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from rpm_contracts import (
    ClinicalEvent,
    Command,
    DeviceEvent,
    Metrics,
    Shadow,
    Telemetry,
    TelemetryTs,
    split_ingest_record,
    uuid7,
)

ROOT = Path(__file__).resolve().parents[3] / "contracts"
SCHEMAS = {p.stem: json.loads(p.read_text()) for p in (ROOT / "schemas").glob("*.json")}
EXAMPLES = sorted((ROOT / "examples").glob("*.json"))

MODEL_FOR_PREFIX = {
    "telemetry": ("rpm.telemetry.v1", Telemetry),
    "device_event": ("rpm.device_event.v1", DeviceEvent),
    "command": ("rpm.command.v1", Command),
    "shadow": ("rpm.shadow.v1", Shadow),
    "clinical_event": ("rpm.clinical_event.v1", ClinicalEvent),
}


def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(SCHEMAS[name], format_checker=FormatChecker())


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_valid_against_schema_and_model(path: Path) -> None:
    schema_name, model = MODEL_FOR_PREFIX[path.name.split(".")[0]]
    doc = json.loads(path.read_text())
    _validator(schema_name).validate(doc)
    roundtrip = model.model_validate(doc).to_dict()
    _validator(schema_name).validate(roundtrip)


def test_model_output_validates_against_schema() -> None:
    now = datetime.now(UTC)
    t = Telemetry(
        device_id="sim-90000000",
        seq=1,
        source="simulator",
        ts=TelemetryTs(window_start=now - timedelta(seconds=30), window_end=now, edge_sent=now),
        metrics=Metrics.model_validate({"heart_rate": {"mean": 80}, "spo2": {"value": 97}}),
    )
    _validator("rpm.telemetry.v1").validate(t.to_dict())


def test_empty_metrics_rejected() -> None:
    with pytest.raises(ValueError):
        Metrics()


def test_unknown_field_rejected() -> None:
    doc = json.loads((ROOT / "examples" / "telemetry.galaxy_watch.json").read_text())
    doc["patient_name"] = "should never be on the wire"
    with pytest.raises(ValueError):
        Telemetry.model_validate(doc)


def test_split_ingest_record() -> None:
    doc = json.loads((ROOT / "examples" / "telemetry.galaxy_watch.json").read_text())
    meta, payload = split_ingest_record({**doc, "thing": "gw-demo-01", "kind": "telemetry", "ingest_ts_ms": 1})
    assert meta.thing == "gw-demo-01" and meta.kind == "telemetry"
    assert "thing" not in payload
    Telemetry.model_validate(payload)


def test_uuid7_is_time_ordered_and_versioned() -> None:
    a, b = uuid7(), uuid7()
    assert a.version == 7 and a.variant == "specified in RFC 4122"
    assert a.int >> 80 <= b.int >> 80
