# Compliance matrix: HIPAA Security Rule and GDPR

The platform processes synthetic data only, but it is built as if the data were PHI and personal health data under
GDPR Art. 9. This matrix maps each requirement to the control that implements it and where the control lives.
Items marked **Org** are organisational duties of the operator (Cloud Box or a hospital) that software cannot
discharge on its own. For attack-oriented analysis see [threat-model.md](threat-model.md).

## HIPAA Security Rule (45 CFR Part 164, Subpart C)

### Administrative safeguards (§164.308)

| Standard | Control in the platform | Where |
|---|---|---|
| (a)(1) Security management: risk analysis | STRIDE threat model with residual risks; dependency CVE scans (pip-audit, npm audit) in CI | `docs/security/threat-model.md`, `.github/workflows/ci.yml` |
| (a)(1) Information system activity review | Immutable application audit log (FHIR AuditEvent) and CloudTrail, both in WORM storage; Grafana dashboards for operational review | `audit.tf`, `audit_export.py`, `deploy/compose/observability` |
| (a)(3) Workforce security / (a)(4) access management | Role-based access: nurse, physician, admin, presenter. Accounts are admin-created only (no self sign-up); roles are Cognito groups | `rpm_api/auth.py`, `cognito.tf`, `make cloud-user` |
| (a)(5) Security awareness and training | **Org** | – |
| (a)(6) Security incident procedures | Runbook: alert storm, device compromise (revoke certificate), stream lag, LLM outage | `docs/runbook.md` |
| (a)(7) Contingency plan | RDS automated backups; infrastructure as code (`cloud-up` rebuilds from scratch); durable edge outboxes; the deterministic safety net keeps alerting without the LLM (chaos-tested) | `data.tf`, `infra/scripts`, `eval/results/chaos.json` |
| (a)(8) Evaluation | Evaluation report: replay (alarm burden, sensitivity), chaos, load, latency | `docs/evaluation-report.md` |
| (b)(1) Business associate contracts | AWS BAA covers the HIPAA-eligible services used: IoT Core, Kinesis, S3, RDS, ElastiCache, SQS, SES, SNS, EKS, Cognito, Bedrock, KMS, CloudTrail | **Org:** accept the AWS BAA in AWS Artifact before real PHI |

### Physical safeguards (§164.310)

| Standard | Control | Where |
|---|---|---|
| Facility access, workstation and device security | AWS data centres (inherited). Edge devices hold no patient identifiers, and keys live in the app sandbox. | AWS SOC reports (inherited); ADR 0005, 0011 |
| Device and media controls | Encryption at rest everywhere (KMS CMK with rotation); EBS encrypted; gateway volume encrypted | `security.tf`, `data.tf`, `icu_gateway.tf` |

### Technical safeguards (§164.312)

| Standard | Control | Where |
|---|---|---|
| (a)(1) Access control: unique user identification | Cognito users; the API authenticates every request with a verified access token and records the username and subject | `auth.py` |
| (a)(2)(iii) Automatic logoff | Access tokens last 60 minutes and refresh tokens 12 hours (one shift); the session lives in the tab (`sessionStorage`) and is cleared on sign-out or when the tab closes | `cognito.tf`, `auth.ts` |
| (a)(2)(iv) Encryption and decryption | KMS for RDS, S3, Kinesis, SQS, ElastiCache, Secrets Manager, logs and EBS | `security.tf` and all data resources |
| (b) Audit controls | `audit_log` records every PHI read and action and is append-only through a trigger; hourly hash-chained export to S3 Object Lock as FHIR AuditEvent; CloudTrail with log-file validation | `0005_audit_hardening.sql`, `audit_export.py`, `audit.tf` |
| (c)(1) Integrity | Object Lock (WORM) for audit; hash chain across export batches; the IoT policy pins each device to its topics; contract validation rejects malformed data | as above; `contracts/` |
| (d) Person or entity authentication | Clinicians: Cognito with optional TOTP MFA (**Org:** set MFA to ON). Devices: an X.509 certificate per thing, with mutual TLS. | `cognito.tf`, `iot.tf` |
| (e)(1) Transmission security | TLS 1.2+ throughout: device mTLS, `rds.force_ssl`, ElastiCache in-transit encryption, S3 deny non-TLS, HSTS on the console | `data.tf`, `streaming.tf`, nginx template |

### Privacy Rule touch-points (§164.502(b), §164.514)

| Requirement | Control | Where |
|---|---|---|
| Minimum necessary | Notifications carry initials, the last 4 characters of the id, severity and a link. The agent context contains only what the task needs. No PHI in IoT topics or payloads. | `notify.py`, `context.py`, ADR 0005 |
| De-identification (dataset) | MIMIC-IV-shaped synthetic data; display names are synthetic | `libs/rpm_mimic` |

## GDPR (EU 2016/679), relevant to Dubai and EU patients

| Article | Requirement | Control | Where |
|---|---|---|---|
| 5(1)(c) | Data minimisation | No identifiers at the edge; PID segments stripped at the gateway; minimum-necessary notifications | ADR 0005, 0011 |
| 5(1)(f), 32 | Integrity, confidentiality, security of processing | Encryption, RBAC, network policies, audit trail, least-privilege IAM per workload | as in the HIPAA table |
| 6(1)(c)/(e), 9(2)(h) | Lawful basis for health data: provision of health care | **Org:** documented in the care pathway; FHIR `Consent` resource planned | – |
| 13–14 | Transparency | **Org:** patient information leaflet for remote monitoring | – |
| 15, 20 | Access and portability | All clinical data is in FHIR R4 (HAPI), so it can be exported through `$everything` / bulk export | `services/fhir-loader`, HAPI FHIR |
| 17 | Erasure | Monitoring data can be deleted per patient. Audit records are retained under a legal obligation (Art. 17(3)(b)); `audit_log` is intentionally immutable. | `0005_audit_hardening.sql` |
| 22 | Automated decision-making | The agents recommend and draft; clinicians decide. The agent may raise severity but never lower it, and only deterministic rules page. | ADR 0001, 0007 |
| 25 | Data protection by design and default | Default-deny network policies, fail-closed authentication, no PHI on devices, synthetic data in all non-production environments | this repo |
| 30 | Records of processing | **Org** (a template is provided by the data-flow section of the threat model) | `threat-model.md` |
| 33–34 | Breach notification | Runbook incident steps; CloudTrail and audit evidence to scope a breach | `docs/runbook.md` |
| 35 | DPIA | Threat model and this matrix are inputs. **Org:** a formal DPIA before real patients. | – |
| 44–49 | International transfers | Single region (us-east-1 in the demo). For UAE or EU patients, deploy to `me-central-1` or an EU region: the IaC is region-parameterised. | `variables.tf` |

## What is out of scope for software

The platform cannot on its own provide:

- a signed BAA;
- workforce training;
- policies and procedures;
- a DPIA;
- patient consent processes.

The items marked **Org** above list these operator obligations.
