# Security and compliance

The platform handles synthetic data only. The Free-plan AWS project cannot accept a HIPAA Business Associate
Addendum, so this document describes controls **aligned with** HIPAA and GDPR, not a compliant deployment.

## Controls

| Area | Control | Where |
|---|---|---|
| Sign-in | Amazon Cognito (authorization code with PKCE) on AWS; locally the Care API mints tokens of the same shape, verified by the same code | `services/care-api/src/care_api/auth.py` |
| Roles | nurse and physician read and act; admin reads, runs demo controls and sees the audit trail; presenter runs demo controls only | same file, `PERMISSIONS` |
| Device identity | One X.509 certificate per device; the IoT policy lets a device publish only to its own topics; the platform takes the device id from the topic, never the payload | IoT policy, stream processor |
| No identifiers at the edge | Device messages carry a device id only; the device-to-patient mapping lives server-side | `contracts/`, `device_assignments` table |
| Service-to-service | Shared service token on `/internal/*` and the FHIR hook; those paths are not exposed through the ingress | `care_api/internal.py`, `care_api/fhir.py` |
| Agent access | Agents read and act only through Care API tools; no database credentials in the agent service; no tool lowers or closes an alert | `care_api/internal.py`, `agent_service/api.py` |
| Agent guardrails | Deterministic policy gate before any action; citations checked against retrieved passages; structured outputs validated | `agent_service/policy.py`, `graph.py` |
| Audit | Every read of patient data and every action is logged with the actor (clinician, service or agent by name); the table refuses UPDATE, DELETE and TRUNCATE; hourly export as hash-chained FHIR AuditEvent files | `audit_log`, `care_api/audit_export.py` |
| Minimum necessary | Alert e-mails carry initials, the last four characters of the id and a console link; no vitals or diagnoses; one message per recipient, so nobody sees who else is on call | `care_api/alerts/notify.py` |
| Encryption in transit | MQTT over TLS with client certificates; HTTPS to the console; TLS to AWS services | IoT Core, CloudFront |
| Encryption at rest | Encrypted instance disk, S3 and SQS | CloudFormation template |
| Browser | `Cache-Control: no-store` on API responses; strict Content-Security-Policy; the WebSocket token travels as a subprotocol, not in the URL | console nginx config, `care_api/main.py` |
| Model safety | Unvalidated models are labelled, cannot page and start no agent work or patient contact | stream processor, Care API engine |

## Mapping to the regulations

| Requirement | How it is met |
|---|---|
| HIPAA §164.312(a) access control | Cognito sign-in, role checks on every endpoint, per-device certificates |
| HIPAA §164.312(b) audit controls | Append-only audit log covering clinicians, services and agents; exported as FHIR AuditEvent |
| HIPAA §164.312(c) integrity | Hash-chained audit exports; idempotent event processing keyed on message id |
| HIPAA §164.312(e) transmission security | TLS on every external hop |
| HIPAA §164.502(b) minimum necessary | Notifications without clinical content; agents read section by section |
| GDPR Art. 5(1)(c) data minimisation | No patient identifiers in device payloads or in notifications |
| GDPR Art. 25 privacy by design | Pseudonymous device ids; mapping held server-side only |
| GDPR Art. 30 records of processing | The audit trail |
| GDPR Art. 32 security of processing | Encryption in transit and at rest, role-based access |

## Known limits

- **CloudFront to the instance is plain HTTP.** HTTPS ends at CloudFront; the hop to the instance is restricted to
  CloudFront's address range but is not encrypted. A domain with a certificate on the instance removes this.
- **One shared service token** rather than one identity per service. Kubernetes network policies limit who can
  reach the internal endpoints.
- **Prompts sent to Amazon Bedrock contain clinical data** (pseudonymous patient id, vitals, record excerpts).
  Bedrock does not use them to train models, and the Australia-only inference profiles keep them in Australia.
- **Model plug-ins run team-written code** inside the risk service. They are trusted code and are reviewed like
  any other change.
- **Single node:** an instance failure stops monitoring until it restarts.
- **No SMS:** the Free plan blocks it, so paging is console and e-mail.
