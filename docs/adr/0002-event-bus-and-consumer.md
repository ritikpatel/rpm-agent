# ADR 0002 — IoT Core → Kinesis, with a lightweight checkpointed consumer

**Status:** accepted · 2026-09-25

## Context
Device messages need durable, ordered-per-device buffering between AWS IoT Core and processing. Options: Kinesis
(+KCL, +Lambda, +Managed Flink), Amazon MSK / MSK Serverless, SQS FIFO.

## Decision
- IoT topic rule → **Kinesis Data Streams** (partition key = thing name → per-device ordering), plus Firehose → S3
  for the raw archive.
- Consumer: `services/stream-engine/kinesis.py`, one thread per shard, checkpoints in Postgres, at-least-once.
  Idempotency comes from `msg_id` (unique in `vitals`, Redis dedupe for events).
- One replica (`strategy: Recreate`) for the demo's 1–2 shards.

## Alternatives rejected (for now)
- **KCL (multi-lang)**: correct at scale but adds a Java daemon + DynamoDB lease table; unnecessary at 1–2 shards.
- **MSK Serverless**: always-on cluster cost is too high for a demo that is torn down between sessions.
- **Managed Flink**: powerful windowing, but cost and learning curve exceed the 4-week budget.

## Upgrade path
At >4 shards or multiple replicas, swap the poller for KCL or a Lambda event-source mapping; the `Processor`
interface (`handle_batch(records)`) stays unchanged.
