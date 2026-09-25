# ADR 0006 — Graviton (arm64) EKS nodes

**Status:** accepted · 2026-09-25

The team builds on Apple-silicon Macs (arm64), and every image in use (our Python services, HAPI FHIR) ships arm64
variants. Running EKS on `t4g` Graviton nodes (`AL2023_ARM_64_STANDARD`) removes cross-compilation from
`cloud-up`, speeds up builds, and costs roughly 20 % less than equivalent x86 instances. RDS (`db.t4g`) and
ElastiCache (`cache.t4g`) follow the same choice.
