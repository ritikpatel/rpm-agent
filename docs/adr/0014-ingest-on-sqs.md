# ADR 0014 — Device ingest on SQS, with an in-cluster S3 archiver

**Status:** accepted · 2026-10-10 · supersedes [ADR 0002](0002-event-bus-and-consumer.md)

## Context

The AWS project that hosts the demo is a "new AWS experience" project on the Free plan, locked to one Region
(`ap-southeast-2`). Its managed service control policy cannot be changed, and it does not include **Kinesis Data
Streams** or **Amazon Data Firehose**: `kinesis:ListStreams` returns an explicit deny. ADR 0002 put both on the
ingest path, so `make cloud-up` could not have created the platform there. The choice was to upgrade the project
to the Paid plan or to rebuild ingest on services the Free plan includes; the project owner chose the second.

The requirements did not change:

- a durable buffer between IoT Core and processing, so a consumer outage loses nothing;
- a raw archive of everything devices send, for retraining and evaluation;
- per-device consistency of the live state (latest vitals, rule state);
- the real-time path must not depend on the archive.

One fact about ordering shaped the design. AWS documents the IoT rules engine as a fully distributed service
that gives no ordering guarantee when it triggers the SQS action, and that action cannot target FIFO queues. Order
therefore has to come from the data. The stream engine already worked in event time for store-and-forward backfill
and late windows; Kinesis kept arrival order per device, which made the out-of-order case rare.

## Decision

```
devices ──MQTT/mTLS──▶ IoT Core rule ──┬─▶ SQS rpm-telemetry ─────────▶ stream-engine ──▶ (unchanged)
                                        └─▶ SQS rpm-telemetry-archive ─▶ telemetry-archiver ──▶ S3 lake
```

1. **Two SQS standard queues, one IoT rule.** The rule keeps its SQL (`thing` from the topic, `ingest_ts_ms`) and
   has two SQS actions. Each queue has a dead-letter queue, is encrypted with the platform KMS key and keeps
   messages for 4 days. The queues are standard because the rule's SQS action does not support FIFO queues.
2. **Stream engine consumer** (`stream_engine/consumer.py`, `rpm_common/sqs.py`). It long-polls, so a message is
   picked up when it arrives. With a backlog it drains up to 1,000 messages with 4 concurrent receives. One thread
   processes batches, because per-device state in Redis is read-modify-write. Messages are deleted after the batch
   is committed (at-least-once; `msg_id` makes processing idempotent).
3. **Order comes from event time, not from the transport.**
   - Each batch is sorted by event time before it is processed.
   - While the consumer is catching up (the oldest message waited more than 5 s, or the drain filled a batch), the
     newest half of each batch is held back and sorted together with the next drain. Nothing is held when the queue
     keeps up, and nothing is held longer than 10 s.
   - A window that still arrives after a newer one for its device takes the processor's existing `late` path: it
     is stored and flagged, and never applied to live state.
   - Sequence-gap detection (`DATA_GAP`) is skipped for records that waited more than 5 s, because a jump in
     sequence numbers inside a backlog is reordering, not loss.
4. **Failures.**
   - Postgres or Redis down: the batch is released and nothing is received until they answer, so an outage does not
     use up receive counts.
   - A batch that fails otherwise is retried record by record. A record that keeps failing moves to the
     dead-letter queue after 10 receives instead of blocking the rest. (The Kinesis consumer retried the same batch
     for ever.)
   - A batch that could not be delivered gives back the alert-suppression slots it took. Before this, a retried
     batch found its own alert already "emitted" and dropped it for the suppression period.
5. **Archive** (`stream_engine/archive.py`, second entrypoint of the stream-engine image). It writes what Firehose
   wrote: gzip, one JSON record per line, `telemetry/dt=YYYY-MM-DD/`, flushed every 60 s or 5 MiB. Messages are
   deleted only after their object is stored, so delivery is at-least-once and readers de-duplicate on `msg_id`.
   Its IAM role can read its queue and write `telemetry/*` in the lake; it has no database access.
6. **Local stack.** ElasticMQ carries the two queues, the rule emulator sends to both, and the same archiver writes
   to `data/lake`. `kinesis-mock` is gone.

`Processor.handle_batch(records)` is unchanged, so the consumer remains the only part tied to the transport.

## Evidence

**Reordering on real SQS.** A throwaway standard queue in `ap-southeast-2` was loaded with a backlog shaped like an
engine outage (150 devices, sent in event-time order) and drained with the production consumer from a laptop.
One run per row, 2026-10-10. "Late" is the processor's own rule: older than the newest window already applied for
that device.

| Backlog | Batch / held back | Late windows | False `DATA_GAP` |
|---|---|---|---|
| 1,200 | 500 / 0 (sort within the batch only) | 130 (10.8%) | 2 |
| 1,200 | 500 / 250 | 0 | 0 |
| 3,000 | 500 / 250 | 225 (7.5%) | 5 |
| 3,000 | 1,000 / 500 (the default) | 0 | 0 |
| 3,000 | 2,000 / 1,000 | 0 | 0 |

No message was lost or duplicated in any run. The hold limit was raised to 60 s for these runs, because one drain
takes about 10 s from a laptop and well under 1 s inside the Region. Larger backlogs than 3,000 were not measured:
expect some late windows there, which is the safe failure (stored, flagged, not applied).

**Local stack** (`docs/evaluation-report.md` §2–4): LOCAL_EVIDENCE

## Consequences

Better:

- Runs on the Free plan, with no per-hour charge for the bus (SQS is billed per request).
- The ingest-to-scored hop no longer waits for a 1 s poll.
- Four days of buffering instead of 24 hours.
- A record that cannot be processed no longer stalls the pipeline.

Worse, or to keep in mind:

- **No replay from the bus.** A processed message is gone. Reprocessing has to read the S3 archive, and no tool
  for that exists yet.
- **The transport does not order.** See the table. After a hard kill, the batch that was in flight returns after
  the visibility timeout (60 s) and is stored as late.
- **One consumer.** Run exactly one stream-engine replica, as before. To go beyond one consumer, shard devices
  across several queues (one IoT rule per shard), or move back to Kinesis on a Paid plan by replacing
  `consumer.py`.
- **The archiver is ours to run.** Firehose was managed; this is 150 lines with tests, metrics and a chaos
  experiment.

## Alternatives rejected

- **Upgrade to the Paid plan and keep Kinesis and Firehose.** Valid, and still the simplest way to get ordered,
  replayable ingest. Not chosen by the project owner.
- **IoT rule → Lambda → SQS FIFO.** FIFO would preserve only the order in which the rules engine invoked the
  function, which is not guaranteed either, and it adds a function to the hot path.
- **IoT rule S3 action for the archive.** One object per message.
- **Lambda with an SQS event source for the archive.** Workable. The archiver stays in the cluster so that the
  local stack, the tests and the chaos experiments run the same code.
- **Stream engine subscribes to IoT Core over MQTT.** No durable buffer while the engine is down.
- **Amazon MQ or MSK.** An always-on broker for a demo that is torn down between sessions; MSK is not in the plan.
