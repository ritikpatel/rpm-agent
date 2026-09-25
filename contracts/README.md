# Contracts

The JSON Schemas in `schemas/` are the **source of truth** for every message crossing a service or device boundary.
Kotlin (watch / phone), Python services and the Greengrass gateway all build against them; `libs/rpm_contracts`
mirrors them as Pydantic models and a test validates every file in `examples/` against both.

| Schema | Topic / channel | Direction |
|---|---|---|
| `rpm.telemetry.v1` | `rpm/v1/dev/{thing}/telemetry` (MQTT QoS 1) | device → cloud |
| `rpm.device_event.v1` | `rpm/v1/dev/{thing}/events` (MQTT QoS 1) | device → cloud |
| `rpm.command.v1` | `rpm/v1/dev/{thing}/cmd` (MQTT QoS 1) | cloud → device |
| `rpm.shadow.v1` | `$aws/things/{thing}/shadow/...` (`state.desired` / `state.reported`) | both |
| `rpm.clinical_event.v1` | SQS `rpm-clinical-events`, `rpm-alert-intake` | stream-engine → agents / alert-service |

## Rules
- **No patient identifiers** in device topics or payloads. `device_id` must equal the IoT thing name; the cloud takes the
  thing name from the MQTT topic (`topic(4)`), never from the payload.
- `msg_id` is a UUIDv7 and the idempotency key; delivery is at-least-once end to end.
- `seq` is a per-device monotonic counter; gaps raise `DATA_GAP`, late store-and-forward data sets `edge.buffered=true`.
- Timestamps are RFC 3339 UTC. `ts.window_end` is the event time used for scoring and latency.
- Breaking changes create a new version (`v2`) and a new topic segment; never mutate a published version.

## IoT rule (AWS and the local emulator implement the same enrichment)
```sql
SELECT *, timestamp() AS ingest_ts_ms, topic(4) AS thing, topic(5) AS kind
FROM 'rpm/v1/dev/+/+' WHERE topic(5) = 'telemetry' OR topic(5) = 'events'
```
Records go to Kinesis `rpm-telemetry` with partition key `thing` (preserves per-device ordering).
