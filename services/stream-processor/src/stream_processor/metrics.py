from prometheus_client import Counter, Gauge, Histogram

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13, 21, 34, 60)

RECORDS = Counter("rpm_records_total", "Records consumed", ["kind", "outcome"])
HOP_LATENCY = Histogram("rpm_hop_latency_seconds", "Per-hop latency (sensor window end -> processed)",
                        ["hop", "context"], buckets=LATENCY_BUCKETS)
EVENTS = Counter("rpm_clinical_events_total", "Clinical events emitted", ["type", "severity"])
SUPPRESSED = Counter("rpm_clinical_events_suppressed_total", "Events suppressed as duplicates", ["type"])
BATCH_SECONDS = Histogram("rpm_batch_seconds", "Time to process one ingest batch",
                          buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5))
QUEUE_AGE = Gauge("rpm_ingest_queue_age_seconds",
                  "Consumer lag: time the oldest message of the last batch spent on the ingest queue")
QUEUE_DEPTH = Gauge("rpm_ingest_queue_depth", "Approximate messages on the ingest queue and its DLQ",
                    ["queue", "state"])
MESSAGES = Counter("rpm_ingest_messages_total", "SQS messages by outcome", ["outcome"])
HELD = Gauge("rpm_ingest_reorder_held", "Records held back for reordering while catching up on a backlog")
CLOCK_SKEW = Counter("rpm_clock_skew_total", "Negative hop durations observed (device clock ahead)", ["hop"])
AWAITING_MODELS = Counter("rpm_findings_awaiting_models_total",
                          "Medium findings for home patients held until a confirming risk model agrees", ["type"])
CONFIRMATIONS = Counter("rpm_model_confirmations_total", "Held findings by outcome (confirmed, not_confirmed, "
                        "unavailable: no model answered, so the rule alone pages)", ["outcome"])
