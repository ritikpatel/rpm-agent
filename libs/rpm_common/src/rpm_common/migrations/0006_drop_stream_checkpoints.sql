-- Device ingest moved from Kinesis (shard checkpoints) to SQS, where delivery state lives in the queue
-- (docs/adr/0014). Nothing reads this table any more.
DROP TABLE IF EXISTS stream_checkpoints;
