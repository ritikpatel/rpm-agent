# Demonstration script

A walk through the live console on AWS that shows every component working. The full version takes about 15 minutes;
a 6-minute version (the sponsor deck's demo slide) is at the end. Every patient on screen comes from the team's
training data; every reading after their recorded data ends is simulated.

## Before the audience arrives (about 15 minutes ahead)

1. Sign in to AWS and start the platform (about 3 minutes; skip what is already running):

   ```bash
   aws login --profile rpm-capstone
   ```
   ```bash
   make cloud-start
   ```
   ```bash
   make cloud-sim-resume
   ```
   ```bash
   make cloud-status
   ```
   Every container should show `Running`.
2. Open the console, https://d3c6834713f45i.cloudfront.net, and sign in with an account in the physician, presenter
   and admin groups (the top bar then reads "Physician" and shows "Audit trail").
3. Tidy the inbox: open each leftover alert, **Resolve** it with a short note. A clean inbox makes the new alert
   obvious. (RPM-10018328 alerts again by itself after a while: its discharge vital signs score NEWS2 5 to 6.)
4. Choose and note three patients:
   - **ICU, calm:** worklist, **ICU** filter, a patient with no alert and NEWS2 low. This one will deteriorate.
   - **ICU, to discharge:** a second calm ICU patient.
   - **Home:** worklist, **Post-discharge** filter, a patient with no alert. Used for the watch and the models.
5. Optional, the watch: start the `wear_test` emulator (Android Studio, Device Manager), wait for "connected" on its
   screen, then give it to the home patient:
   ```bash
   make cloud-assign P=<home patient id> D=wear-emu-01
   ```
6. Open in other tabs: the mailbox of the care-team tier you will show (the first page goes to the tier that matches
   the severity), and the sponsor deck at the architecture slide.

## The demonstration (about 15 minutes)

| Min | Component | Do | Say |
|---|---|---|---|
| 0–1 | Architecture | Deck, slide 4 | "One straight path: devices to AWS IoT Core, one queue, a rules engine and the team's three models, the Care API that pages people, and four AI agents that write the handover. The paging path has no AI on it." |
| 1–2 | Sign-in and roles | The top bar | "Sign-in is Amazon Cognito. What you may do depends on your role: nurse, physician, admin, presenter. People appear by role, never by name, here and in every timeline." |
| 2–4 | Real-time monitoring | Worklist: the tiles, then the **ICU** and **Post-discharge** filters; click the **Active alerts** tile | "Thirty patients: six on ICU bedside monitors, twenty-four at home on a watch. The census updates live: NEWS2 (the asterisk means a wearable measures it only in part), the short-term risk models and the 30-day risk, a heart-rate trend. The tiles are clickable: active alerts, or only the critical ones." |
| 4–6 | Patient record and risk models | Open the home patient: **Vital signs** (switch the time range), **Record summary**, **Lab results**, **Risk models** | "The chart starts with the patient's recorded data after discharge; live monitoring picked up where it ended. The risk models are grouped by what they look ahead to. Recovery monitoring is validated. Early deterioration we retrained on the shared data this week, and it is validated too. Readmission over 30 days is a research model. The line on top says whether the short-term models agree; we never average them." |
| 6–8 | Early warning | Open the calm ICU patient, **Demo controls**: scenario **sepsis**, ramp 60, **Start scenario**. Watch **Vital signs**, then the worklist inbox and the mailbox | "The heart rate climbs and the oxygen falls. The rules score NEWS2 on every reading. There: the alert, in under a second from the reading that crossed the line, and the e-mail. If nobody acknowledges it, it climbs from the RPM nurse to the physician to rapid response on a timer." |
| 8–10 | Agentic AI and RAG | On the alert: **Agent summary (SBAR)** (about a minute after the alert), click a citation tag, **Agent assessment**, **Timeline** | "While the page went out, four agents worked: Monitoring, Risk, Escalation and Summary, on Claude through Amazon Bedrock. The summary is in SBAR form and every claim is cited: this opens the protocol passage it came from. A citation that matches nothing is rejected. The timeline shows what each agent did, under its own name. They may raise urgency; they can never lower or close an alert." Then **Acknowledge** with a note. |
| 10–11 | Smart-hospital connectivity | Home patient: **Ask: short of breath?**, **Request SpO₂ reading**, type a message and **Send**. Point at **Messages with the patient** (and the emulator screen if used) | "The clinician reaches the patient on the watch. The answer comes straight back here. A 'yes' to a symptom question raises the alert, or opens one. The watch also has an SOS button." Optionally press **SOS** on the emulator: a critical alert opens at once. |
| 11–12 | Hospital record in (FHIR) | Home patient, **Demo controls**: lab Lactate 4.2 mmol/L, flag H, **File lab result**. Point at **Lab results** and **Clinical events** | "The lab is filed in the hospital's FHIR server, the way a lab system would. The platform hears about it through a FHIR subscription, adds it to the patient's events and rescores the risk models." |
| 12–13 | Discharge | Second ICU patient, **Demo controls**, **Discharge home** | "The discharge happens in the hospital record. The platform follows: the bedside monitor stops, a watch takes over, the readmission model scores the patient, and the agents write a discharge summary." |
| 13–14 | Audit and compliance | **Audit trail** in the top bar | "Every read of patient data and every action is here: clinicians and services, and each agent by name. The log refuses changes and is exported every hour to S3 with a hash chain, so tampering would show." |
| 14–15 | Hospital record out (FHIR) | In a terminal: `make cloud-fhir Q='Flag?patient=<ICU patient id>'` (or `RiskAssessment?patient=…`) | "What we produce goes back to the hospital as standard FHIR resources: the alert as a Flag, the scores as RiskAssessments, the summary as a document. A resolved alert's Flag is closed." |

Close with the deck's coverage and road-to-pilot slides.

## The 6-minute version (sponsor deck, slide 15)

1. **Worklist** (1 min): the tiles, the two filters, the census columns.
2. **A deterioration** (1.5 min): sepsis on the calm ICU patient, ramp 30; the page and the e-mail.
3. **The agents' summary** (1 min): on that alert; click one citation.
4. **A home patient's risk models** (1 min): the two horizons and the agreement line.
5. **The watch** (1 min): **Ask: short of breath?** and the answer; SOS if the emulator is running.
6. **A discharge** (0.5 min): **Discharge home** on the second ICU patient.

## After the demonstration

- On the deteriorated patient: **End** the scenario, then **Resolve** the alert.
- If the emulator was used: `make cloud-assign P=<home patient id> D=sim-<home patient id>`, then close the emulator.
- To stop spending (data is kept; `make cloud-start` brings it back in about 2 minutes):
  ```bash
  make cloud-sim-pause
  ```
  ```bash
  make cloud-stop
  ```

## If something goes wrong

| Problem | What to do |
|---|---|
| No alert after a minute | Check the ramp has run (ICU patient's **Vital signs**); a scenario on a home patient pages later (home NEWS2 must persist 10 minutes). Use an ICU patient. |
| The summary is labelled "offline rules" | Amazon Bedrock was unreachable; the summary comes from deterministic rules. Say so: paging never depended on it. |
| "No agent summary was started" | The daily limit of automatic agent runs was reached, or the agents reviewed this patient moments ago. Click **Ask the agents for a review**. |
| No e-mail | The SES sandbox only delivers to verified addresses; the console page is the primary channel. |
| The console does not load | `make cloud-status`; if the instance is stopped, `make cloud-start`. As a last resort, use the screenshots on deck slides 3, 10 and 11. |
| AWS commands fail with an expired session | `aws login --profile rpm-capstone`. |
