# ADR 0003 — Local stack mirrors AWS with open-source stand-ins

**Status:** accepted · 2026-09-25

## Decision
| AWS | Local |
|---|---|
| IoT Core + topic rule | Mosquitto + `services/iot-bridge` (same SQL semantics: `thing` from the topic, `ingest_ts_ms`) |
| Kinesis | `etspaceman/kinesis-mock` |
| SQS | ElasticMQ |
| RDS PostgreSQL + pgvector | `pgvector/pgvector:pg16` |
| ElastiCache | Redis 7.4 |
| HAPI FHIR on EKS | the same HAPI image |

Services only see `RPM_*_ENDPOINT` overrides; the application code paths are identical to AWS.

## Why not LocalStack
Licensing and authentication requirements for LocalStack images have been changing, and IoT Core emulation is not
in its free tier. The stand-ins above are free and stable, and together they cover every service used in week 1.
