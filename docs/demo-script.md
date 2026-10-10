# Demonstration script (10 minutes)

Before the audience arrives: `make up LLM=bedrock MAIL=ses`, `make seed`, `make sim`, sign in as **Demo lead**,
and keep your mailbox open in a second tab. Every patient on screen is from the team's training data. Pick one ICU
patient with no alert (for example `RPM-10001725`) and note which home patients are already alerting from their own
data (for example `SYN-000118`).

| Min | Show | Say |
|---|---|---|
| 0–1 | Architecture diagram | "One straight path: devices, one queue, a rules engine, three risk models, four agents, the care team." |
| 1–2 | Worklist, ICU and Post-discharge filters; open a home patient that is already alerting | "These are the patients our models were trained on. Admitted patients are on bedside monitors. Discharged patients wear a watch, and what you see is their own data: this one deteriorates in the dataset, and the platform caught it." |
| 2–3 | A patient's Risk models card | "Three models from our team: early deterioration, readmission risk, recovery monitoring. Each is a plug-in; these are placeholders until the trained models are dropped in." |
| 3–5 | Start `sepsis` on the ICU patient, ramp 60 | "Watch the vitals turn. The rules engine scores NEWS2 on every reading." Wait for the alert. "The care team was paged in under a second, by rules alone. No AI on that path." Show the e-mail. |
| 5–7 | Agent summary on the alert | "Meanwhile four agents worked: what is happening, how serious, who must act, and a handover summary. Every claim is cited; click one and you see the protocol text. The agents raised the sampling rate and escalated; they cannot lower an alert." |
| 7–8 | Discharge the other ICU patient | "Discharge happens in the hospital record, over FHIR. The platform reacts: the ICU monitor stops, the watch takes over, the readmission model runs." |
| 8–9 | Home patient: send a message, ask a check-in question | "The clinician reaches the patient on the watch, and the answer comes back." |
| 9–10 | Audit trail, then FHIR Flag and RiskAssessment | "Every read is audited, including each agent by name. And what we produce goes back to the hospital as standard FHIR resources." |

If the network or Bedrock is unavailable, the same script works with plain `make up`: the summary is then produced
by deterministic rules and labelled "offline-rules".
