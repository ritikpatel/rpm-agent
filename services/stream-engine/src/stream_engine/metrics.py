from prometheus_client import Counter, Gauge, Histogram

LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13, 21, 34, 60)

RECORDS = Counter("rpm_records_total", "Records consumed", ["kind", "outcome"])
HOP_LATENCY = Histogram("rpm_hop_latency_seconds", "Per-hop latency (sensor window end -> processed)",
                        ["hop", "context"], buckets=LATENCY_BUCKETS)
EVENTS = Counter("rpm_clinical_events_total", "Clinical events emitted", ["type", "severity"])
SUPPRESSED = Counter("rpm_clinical_events_suppressed_total", "Events suppressed as duplicates", ["type"])
BATCH_SECONDS = Histogram("rpm_batch_seconds", "Time to process one Kinesis batch",
                          buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5))
MILLIS_BEHIND = Gauge("rpm_kinesis_millis_behind_latest", "Consumer lag", ["shard"])
CLOCK_SKEW = Counter("rpm_clock_skew_total", "Negative hop durations observed (device clock ahead)", ["hop"])
