# Contracts

The JSON Schemas in `schemas/` are the **source of truth** for every message a device sends or receives. The watch
app (Kotlin) and the Python services both build against them; `libs/rpm_core/src/rpm_core/contracts` mirrors them as
Pydantic models and a test validates every file in `examples/` against both.

| Schema | Topic | Direction |
|---|---|---|
| `rpm.telemetry.v1` | `rpm/v1/dev/{thing}/telemetry` (MQTT QoS 1) | device → cloud |
| `rpm.device_event.v1` | `rpm/v1/dev/{thing}/events` (MQTT QoS 1) | device → cloud |
| `rpm.command.v1` | `rpm/v1/dev/{thing}/cmd` (MQTT QoS 1) | cloud → device |
| `rpm.shadow.v1` | `$aws/things/{thing}/shadow/...` | both |
| `rpm.clinical_event.v1` | stream processor → Care API (`POST /internal/events`) | internal |

Two more message types exist only inside the platform and are defined in the Pydantic models: `rpm.ehr_event.v1`
(admission, discharge or lab result from the FHIR hook, on the same queue as device data) and `rpm.agent_task.v1`
(Care API → agent service).

## Rules
- **No patient identifiers** in device topics or payloads. `device_id` must equal the IoT thing name; the platform
  takes the thing name from the MQTT topic, never from the payload.
- `source` is `icu_monitor` for bedside monitors and `context` is `icu`; wearables use `context: home`. The platform
  refuses ICU data for a patient at home and wearable data for a patient in the ICU.
- `msg_id` is a UUIDv7 and the idempotency key; delivery is at-least-once end to end.
- `seq` is a per-device counter; gaps raise `DATA_GAP`, late store-and-forward data sets `edge.buffered=true`.
- Timestamps are RFC 3339 UTC. `ts.window_end` is the event time used for scoring and latency.
- Breaking changes create a new version (`v2`) and a new topic segment; a published version is never changed.

## IoT rule (AWS and the local emulator apply the same enrichment)
```sql
SELECT *, timestamp() AS ingest_ts_ms, topic(4) AS thing, topic(5) AS kind
FROM 'rpm/v1/dev/+/+' WHERE topic(5) = 'telemetry' OR topic(5) = 'events'
```
Records go to the SQS queue `rpm-patient-events`. SQS does not order messages: the stream processor orders by `ts`
and de-duplicates on `msg_id`.
