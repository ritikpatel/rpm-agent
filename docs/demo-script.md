# Demo script (about 15 minutes, plus Q&A)

A presenter run-sheet: what to click, what the audience should see, and what it proves. Every step has a fallback.
Run it twice before the day, with a dry run on the target environment (AWS or local).

## Before the audience arrives (T−30 min)

| Check | Command | Expect |
|---|---|---|
| Environment up | AWS: `make cloud-up`, then `make cloud-board`. Local: `make up && make sim && make obs` | Console at http://localhost:3000 |
| Users | AWS: `make cloud-user U=nurse.demo E=… R=nurse`, plus a physician and a presenter | Invitation e-mails received; first sign-in done (passwords set) |
| Watch | Galaxy Watch on the presenter's wrist, provisioned (`edge/android/provision.sh aws`); or the Wear OS emulator (`provision.sh local`) | Daniel Haddad (p-90000006) streaming, "live" on the console |
| Dashboards | Grafana "RPM · latency and alerting" | Sensor→page p95 < 5 s, failed pages 0 |
| E-mail sink | SES inbox (AWS) or Mailpit http://localhost:8025 | Empty or recent |
| Browser tabs | 1. console (nurse) · 2. console (presenter, private window) · 3. Grafana · 4. inbox · 5. ICU gateway `…:8081/status` · 6. `docs/evaluation-report.md` | – |

## Run-sheet

| Min | Step | Do | Audience sees | Claim it proves |
|---|---|---|---|---|
| 0–1 | Context | HLD diagram (`docs/HLD.md` §2) | Edge → IoT → stream → rules and agents → people | End-to-end architecture |
| 1–2 | Sign-in | Tab 1: sign in as the nurse (Cognito hosted UI) | Worklist: 150 patients, NEWS2 bands, live sparklines | Real authentication and RBAC; real-time fleet |
| 2–4 | The watch | Point at the watch; open Daniel Haddad | HR on the wrist and on the console within a second; per-hop latency in Grafana | Edge-to-cloud path, sub-second |
| 4–7 | Home deterioration | Tab 2 (presenter): inject `sepsis` on a home patient, ramp 90 s | Within about 2–5 min: alert in the inbox; e-mail page; agent brief (SBAR) with citations. Click a citation to see the protocol text. | Rules page; the agent explains with evidence; minimum-necessary e-mail |
| 7–8 | Agent acts | Same patient: the timeline shows the agent's check-in or spot-check | Question on the patient's device; "Yes" reply in the timeline | Closed loop through the device, within policy |
| 8–9 | SOS | Press SOS twice on the watch | Rapid-response page about 1.5 s later; watch buzzes "care team alerted" | Critical path latency |
| 9–11 | ICU | Tab 5: gateway status. Inject `hypoxia` on an ICU bed | Bedside alarm at the gateway; cloud alert judged against the patient's baseline; no patient-facing actions (ICU guardrail) | HL7 edge gateway; context-aware ICU alerting |
| 11–12 | Resilience | Stop the agent service, then SOS again (`docker stop rpm-agent-service-1` or `kubectl scale deploy/agent-service --replicas=0`) | Still paged in about 1.5 s; the brief arrives when the agent is back | Safety net independent of the LLM |
| 12–13 | Security | Tab 2 (presenter) opens a patient: refused. Show `audit_log`: every read by the nurse, with role and IP. | RBAC and immutable audit | HIPAA technical safeguards |
| 13–15 | Evidence | `docs/evaluation-report.md`: alarm burden table, chaos results, latency | Burden cut 88% (home) and 83% (ICU) with unchanged sensitivity; chaos 4/4 | Quality proven, not asserted |

## Fallbacks

| If… | Then… |
|---|---|
| The watch will not connect (venue Wi-Fi) | Use the phone hotspot, or switch to the Wear OS emulator with scripted scenarios (`--es scenario hypoxia`) |
| AWS is unavailable | Run the whole demo on the local stack; it is the same code and the same console |
| An alert is slow to fire (NEWS2 persistence is 10 min at home) | Use `hypoxia` or `af_rvr` (SpO₂ and HR rules fire in about 1–4 min), or SOS |
| No Claude access | Agents run in offline mode: the same policy gate, deterministic cited briefs. Say so; it demonstrates ADR 0001. |
| Mail delay | Show the page in the alert's "Notifications" table (sent timestamp and latency) |

## Likely questions

| Question | Where to point |
|---|---|
| "How do you know it is not just noise?" | Evaluation report §1 (replay with ground truth; PPV, burden, the sweep) |
| "What if the LLM hallucinates?" | Guardrail red-team set 13/13; citation checker; the LLM can never page or lower severity (ADR 0001, 0007) |
| "HIPAA?" | `docs/security/compliance-matrix.md`, threat model, audit export |
| "Scale?" | Load test locally; AWS sizing in evaluation §4; SQS ingest with batched parallel receives (ADR 0014) |
| "Where do the data team's models go?" | ADR 0009: ONNX package contract, `validated` flag, FHIR RiskAssessment |
