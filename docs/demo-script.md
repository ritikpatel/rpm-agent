# Demonstration script: sponsor demo for Laxmi, Cloud Box Technologies

**The goal of the demo:** a watch is attached to a patient in front of the sponsor, the patient's readings on that
watch deteriorate, and the platform raises the alert by itself, pages the care team, has the AI agents write a cited
handover, and lets the clinician reach the patient on the watch. Everything else supports that story.

About 25 minutes: 3 of framing, 13 of live demonstration, 7 of results and next steps, then questions. The sponsor
deck (16 slides plus the team) carries the framing and the results; the console carries the middle.

## The patients

| Role in the demo | Patient | Why this one |
|---|---|---|
| **Wears the watch (the emulator)** | **Zara Okafor, RPM-10007928**, at home | Sepsis: her heart-rate limit at home is 110, so the watch's deterioration alerts within about 4 minutes. Not COPD (whose SpO₂ limit is 88%). Has a hospital record, so her record, labs and risk models are full. No alert open. |
| Discharged during the demo | **Omar Bello, RPM-10024043**, in the ICU | Calm (NEWS2 0), no alert. |
| Backup for the watch | Tariq Khan, RPM-10004422, at home | If Zara Okafor has an alert open. Heart-rate limit 120, so about a minute slower. |
| Avoid | Sofia Mehta, RPM-10018328 | Raises an alert by herself from time to time (her discharge vital signs score NEWS2 5 to 6). |

Check the day before that each has no open alert (open the patient; "No active alert for this patient").

## The emulator's controls

Scroll down the watch screen past **SOS**: three chips, **scenario: normal**, **scenario: deteriorate** and
**scenario: hypoxia**. Deteriorate raises the heart rate by about 48 beats a minute and lowers SpO₂ by about 8 points
over 3 minutes; hypoxia lowers SpO₂ by about 13 points. The watch measures SpO₂ only every 5 minutes on its own, or
when the console asks (**Request SpO₂ reading**, about 15 seconds). A check-in question appears at the top of the
watch with **Yes** and **No**.

## Ready check, 10 minutes before (signed in, emulator running)

1. **Platform up:** in a terminal, `make cloud-status`: every container `Running`. If not: `make cloud-start`, then
   `make cloud-sim-resume`.
2. **Emulator:** the watch screen shows "● connected", and at the bottom "wearos_emulator · direct". Tap
   **scenario: normal** so it starts calm.
3. **Clean inbox:** worklist, open each active alert and **Resolve** it ("cleared before demo"). A clean inbox makes
   the new alert unmistakable.
4. **Tabs:** console worklist; your mailbox (your address receives every tier's page); the deck at slide 1.
5. **Zara Okafor's page** open in a second console tab, scrolled to the **Watch** card. Her header says
   "device sim-RPM-10007928" (the simulator).

## The flow

### 1. Opening, 1 minute (deck slide 1, then 2)

**Say:** "Laxmi, thank you for your time. Your brief asked for a platform that watches patients continuously, in the
ICU and at home after discharge, spots deterioration early and gets the right clinician involved fast. In the next
twenty-five minutes I will show you exactly that, live on AWS: we will put a watch on a patient, make her
deteriorate, and watch the platform react: the alert, the page to the care team, the AI agents' handover and the
clinician talking to the patient on the watch. Then the results, honestly, against your eleven deliverables, and what
a pilot would need."

### 2. The architecture in one picture, 2 minutes (deck slide 4, a glance at slide 5)

**Say:** "One reading's journey. The watch publishes to AWS IoT Core, which only accepts devices holding their own
certificate. The reading joins one queue with everything else, including admissions and lab results from the hospital
record over FHIR. A rules engine scores it in milliseconds. If the patient needs a human, the Care API pages the care
team, and only then do four AI agents review the case. The top row, the paging path, has no AI on it: safety never
waits for a language model. Keep this picture in mind; you are about to see every box light up."

### 3. The console, 1 minute (console worklist)

**Do:** show the tiles; click **ICU**, then **Post-discharge**, then **All patients**.

**Say:** "This is what the care team sees. Thirty patients from our team's data: six in the ICU on bedside monitors,
twenty-four at home on a watch. Each row updates live: NEWS2, our short-term and 30-day risk models, the heart-rate
trend. The tiles are live counters; clicking one filters the inbox."

### 4. The patient, before, 1.5 minutes (Zara Okafor's page)

**Do:** open Zara Okafor: point at the header, **Vital signs**, **Record summary**, **Lab results**, **Risk models**.

**Say:** "Zara Okafor, discharged after an admission with sepsis. The chart begins with her recorded data after
discharge; monitoring continues from there. Her record comes from the hospital system over FHIR. Her three risk
models are grouped by what they look ahead to: recovery and deterioration over the next hours and days, readmission
over thirty days. All calm today. Right now her readings come from our simulator; let's give her a real watch."

### 5. Attach the watch, 1 minute (the key moment)

**Do:** in the **Watch** card type `wear-emu-01`, click **Assign watch**. Within 15 to 30 seconds the header reads
"device wear-emu-01" and new points on **Vital signs** come from the watch (heart rate about 76).

**Say:** "This watch runs our own Wear OS app. Assigning it takes seconds: the watch never knows who wears it; it
sends only its device id, and the patient link lives on our side, which keeps identities off the device. Every
assignment is audited. Those new points are coming from the watch you see, over the internet, through AWS IoT
Core."

### 6. The deterioration, 4 minutes

**Do:** on the watch, scroll down and tap **scenario: deteriorate**. Keep the console's **Vital signs** and the
watch side by side. After about **3 minutes**, click **Request SpO₂ reading** in **Actions**; the watch shows
"measuring…".

**Expect:** the heart rate climbs on the watch and on the chart (76, then past 100, then 110 to 125); NEWS2 in the
header rises. About 15 to 30 seconds after the SpO₂ request, SpO₂ about 89% arrives: a **Low SpO₂** alert opens.
About a minute later the heart-rate rule adds **Sustained tachycardia** (above 110 for 2 minutes).

**Say, while it climbs:** "Her heart rate is climbing, the way it does in early sepsis. The rules engine is
judging every 30-second window against her own limits: for a sepsis patient at home, 110. It also knows motion, so a
fast heart rate during exercise is not an alarm." **When you request SpO₂:** "The watch measures oxygen
occasionally to save battery; a clinician, or the agents, can ask for a reading now." **When the alert opens:**
"There it is: the alert opened on the watch's own reading, in under a second, and paged the care team. Let me show
you the page."

### 7. The page and escalation, 1 minute (worklist and mailbox)

**Do:** click the **Active alerts** tile; open the mailbox and show the e-mail.

**Say:** "Every clinician's console shows it instantly, and the on-call tier gets an e-mail. Notice the e-mail has
no clinical detail: initials, the last digits of the id, a link to the console; that is deliberate, for privacy. If
nobody acknowledges, it climbs on a timer: RPM nurse, physician, rapid response team."

### 8. The agents' handover, 2 minutes (the alert on Zara Okafor's page)

**Do:** about a minute after the alert, **Agent summary (SBAR)** appears. Click one citation tag; show
**Agent assessment** and **Timeline**.

**Say:** "While the page went out, four agents worked on Amazon Bedrock: Monitoring asked what is happening and
whether the signal is trustworthy, Risk weighed her record and the models, Escalation decided who must act, and
Summary wrote this handover in SBAR form. Every statement is cited; this opens the protocol passage it came from. A
summary that cites something we did not retrieve is rejected. The timeline shows what each agent did under its own
name. They can raise urgency; they can never lower or close an alert."

### 9. Talking to the patient, 1.5 minutes (the watch)

**Expect:** the agents usually send the check-in "Are you more short of breath than usual right now?"; it appears at
the top of the watch. If it has not appeared, click **Ask: short of breath?** in **Actions**.

**Do:** tap **Yes** on the watch. Point at **Messages with the patient** and the alert: severity goes up one level and
a higher tier is paged. Optionally type a message ("A nurse will call you in 5 minutes") and **Send**: it appears on
the watch.

**Say:** "The clinician, or the agents, can reach the patient on the watch. She says she is more breathless:
that answer comes straight back, and the platform raises the alert's urgency and pages the next tier. This is the
smart-hospital connectivity in your brief: the doctor can be anywhere and still reach the patient."

### 10. The hospital record, both ways, 1.5 minutes

**Do:** on Zara Okafor's page, **Demo controls**: lab Lactate 4.2 mmol/L, flag H, **File lab result**. Then open Omar
Bello and click **Discharge home**.

**Say:** "A raised lactate, filed in the hospital's FHIR server the way a lab system would. The platform hears it
through a FHIR subscription, adds it to her events and rescores her models. And a discharge: done in the hospital
record, and the platform follows by itself. The bedside monitor stops, a watch takes over, the readmission model
scores him and the agents write a discharge summary."

### 11. The risk models after, 0.5 minute (Zara Okafor's **Risk models**)

**Say:** "Compare with five minutes ago: the models have rescored on the watch's readings and the lab. Each answers
its own question over its own horizon. The console tells you whether the two short-term models agree; it never
averages them."

### 12. Accountability, 1 minute (**Audit trail**)

**Say:** "Every read and every action: clinicians and services, and each agent by name, including the watch
assignment you saw. The log refuses edits and is exported hourly with a hash chain."

Optional, if Laxmi is technical, in a terminal: `make cloud-fhir Q='Flag?patient=RPM-10007928'`: "and the alert
went back into the hospital record as a standard FHIR Flag."

### 13. Results and next steps, 7 minutes (deck slides 7, 13, 14, 16)

- **Slide 7:** "What you saw on one patient we measured on all 263 in the data: 55 of 56 deteriorating patients
  alerted, none of 116 stable ones, about 0.6 alerts per patient per day."
- **Slide 13:** the brief's three measures: accuracy, latency, and why clinical outcomes need a real study.
- **Slide 14:** "Your eleven deliverables: seven delivered, four in part; here is exactly what is missing."
- **Slide 16:** the road to a pilot and what we ask of CBT. Then the team slide and questions.

## After the demo

1. On the watch: **scenario: normal**.
2. On Zara Okafor's alert: **Resolve** ("demo").
3. Give her back to the simulator, then close the emulator:
   ```bash
   make cloud-assign P=RPM-10007928 D=sim-RPM-10007928
   ```
4. Omar Bello stays at home; that is fine (the next seeding restores the cohort).
5. To stop the cost (data kept):
   ```bash
   make cloud-sim-pause
   ```
   ```bash
   make cloud-stop
   ```

## If something goes wrong

| Problem | What to do, and what to say |
|---|---|
| The header still shows the simulator after a minute | The watch is not reporting: check "● connected" on the watch. Reassign, or run `make cloud-assign P=RPM-10007928 D=wear-emu-01`. |
| No SpO₂ after the request | The heart-rate alert still opens at about 4 to 5 minutes. For a faster, critical alert: tap **scenario: hypoxia**, wait 3 minutes, **Request SpO₂ reading** (about 84%: critical). |
| No check-in on the watch | Click **Ask: short of breath?** yourself. |
| The summary says "offline rules" | Bedrock was unreachable; the summary comes from deterministic rules. "Paging never depended on it." |
| "No agent summary was started" | Click **Ask the agents for a review** on the alert. |
| No e-mail | SES sandbox delivers only to verified addresses; the console is the primary channel. |
| The console does not load | `make cloud-status`; if stopped, `make cloud-start` (about 3 minutes). Meanwhile, deck slides 3, 10 and 11 show the screens. |
| AWS commands fail with an expired session | `aws login --profile rpm-capstone`. |

## The 6-minute version (when time is short)

Slide 4 for one minute; attach the watch (step 5); **scenario: hypoxia**, wait 3 minutes while showing the worklist,
**Request SpO₂ reading**: a critical alert; the page; the agents' summary; tap **Yes** on the check-in.
