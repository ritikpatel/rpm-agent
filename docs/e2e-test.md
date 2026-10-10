# End-to-end test script

Follow this top to bottom to see every part of the platform work on the patients in the team's training data. It
takes about 25 minutes. Part A is automated; Parts B to D are the same journey by hand, one patient at a time.

The steps are written for the local stack. On AWS the console address and the sign-in differ (see
[runbook.md](runbook.md)); everything else is the same.

## Before you start

The two training files must be in `Training Data/` in the repo root.

```bash
aws login --profile rpm-capstone        # needed for the Claude agents and for real e-mail
make setup && make models               # once
make up LLM=bedrock MAIL=ses            # Claude agents, alert e-mail to the care team's mailboxes
make seed                               # loads 30 of the training patients
make sim                                # starts their devices
```

Variants: plain `make up` runs the agents offline and keeps e-mail in a local inbox at <http://localhost:8025>
(no AWS sign-in needed). `make mail-status` shows which e-mail addresses Amazon SES will deliver to.

Open <http://localhost:3000> and sign in as **Demo lead** (clinical actions, demo controls and the audit trail in
one account).

### Who you will see

`make cohort` lists them. All 30 are in the training data:

| Group | Patients | Device | What plays |
|---|---|---|---|
| ICU (6) | real MIMIC patients, for example `RPM-10001725`, `RPM-10005348`, `RPM-10014078` | `sim-icu-<id>` | A bedside monitor centred on the vital signs charted at their discharge |
| Home, MIMIC (8) | for example `RPM-10004422`, `RPM-10007928` | `sim-<id>` | Their own post-discharge snapshots (1 to 24 hours) |
| Home, synthetic (16) | for example `SYN-000160`, `SYN-000118`, `SYN-000143` | `sim-<id>` | Their own 72-hour wearable stream |

**Expect alerts within a few minutes of starting, without doing anything.** Several home patients are in a
deteriorating stretch of their own data (for example `SYN-000118`, `SYN-000128`, `SYN-000143`, `RPM-10007928`:
SpO2 around 89%, heart rate above 110). That is the data, not a fault.

## Part A: automated check

```bash
make e2e
```

One PASS or FAIL line per check, about four minutes. It covers live data, risk scores, a deterioration that pages
the care team, the agents' cited summary, FHIR write-back, messaging, a lab result arriving through the hospital
record, a discharge, the audit trail and latency. It uses one ICU patient for a scenario and discharges another;
`make reset up seed sim` gives a fresh cohort.

## Part B: a patient at home who deteriorates in the data

Use `SYN-000118` (or any home patient already showing an alert).

1. **Find the patient.** In the worklist, filter **Post-discharge** and open `SYN-000118`. The header reads
   "synthetic patient · home, discharged …" and the device is `sim-SYN-000118`.
2. **Read the vitals.** Heart rate arrives every 30 seconds; SpO2, respiratory rate and temperature every 5 minutes;
   blood pressure every 15. These are the patient's own values from the wearable stream, at the point in their
   course they have reached. There is no ICU monitor data.
3. **See the alert.** **Active alert** shows what the rules found (for example "NEWS2 high — NEWS2 9") with its
   severity and tier. Under **Notifications**, "Cause → sent" is the time from the reading to the page: well under
   a second.
4. **Check your mailbox.** An e-mail "[RPM HIGH] Patient X.Y. (…0118) needs review" arrives from the sender address
   (look in Junk the first time). It carries initials and the last four characters of the id only. Teammates whose
   address is not yet verified are listed under Notifications as "not delivered".
5. **Read the agents' work.** After about a minute **Agent summary (SBAR)** fills in. Click a citation such as
   `P-NEWS2#2` to open the protocol passage it relies on; the badge reads "citations passed". **Agent assessment**
   gives the concern level; **Timeline** shows what the agents did (for example asked the patient a check-in
   question, raised the sampling rate, or escalated).
6. **Watch escalation.** Leave the alert unacknowledged: after the tier's timer (5 minutes with real e-mail, 1
   minute with the local inbox) the tier rises and the next tier is e-mailed.
7. **Act.** Click **Acknowledge** (the timer stops), then **Resolve** with a reason.
8. **Talk to the patient.** In **Messages with the patient** send a short message; in **Actions** click
   **Ask: short of breath?** The simulated patient's answer appears within about 20 seconds.
9. **Risk models.** The card shows early deterioration, readmission risk and recovery monitoring for this patient,
   each with a probability, a band and the factors behind it, labelled "placeholder, not validated".

## Part C: a patient in the ICU, then discharged

Use `RPM-10001725` (a real MIMIC patient shown as admitted).

1. **Open the patient** from the **ICU** filter. The header shows the diagnosis from the data, "MIMIC patient · ICU",
   and device `sim-icu-RPM-10001725`. Every vital sign updates every 10 seconds, centred on the values charted at
   discharge. Only the early-deterioration model applies.
2. **Make them deteriorate.** In **Demo controls** choose `sepsis`, ramp 60, **Start scenario**. Heart rate and
   respiratory rate climb, blood pressure falls, NEWS2 rises; within about a minute an alert opens, the care team
   is paged and the agents summarise. This adds offsets to this patient's own values; it creates no patient.
3. **End** the scenario and **Resolve** the alert.
4. **File a lab result.** In **Demo controls** leave Lactate 4.2 mmol/L, flag H, and click **File lab result**. It
   appears under **Lab results** within seconds. It was written to the hospital record as a FHIR Observation and
   came back through the same queue as device data.
5. **Discharge.** Click **Discharge home**. Within seconds the header changes to "home, discharged today" and the
   device to `sim-RPM-10001725`. The ICU monitor has stopped: blood pressure and temperature no longer arrive every
   10 seconds. The wearable now plays this patient's own post-discharge data from hour 0.
6. **After discharge.** **Risk models** gains **Readmission risk** immediately and **Recovery monitoring** within a
   few minutes. With the agents on, a discharge summary appears under **Latest agent summary**.

**Admit to ICU** on a home MIMIC patient does the reverse.

## Part D: checks across the platform

1. **One reading from the command line** (the same command works against AWS):
   ```bash
   make send P=SYN-000160 ARGS="--hr 150 --spo2 84"
   ```
   ```bash
   make send P=SYN-000160 ARGS="--event SOS"
   ```
   The SOS opens a critical alert at once and pages tier 3.
2. **The hospital record.** Open these with a patient id from the console:
   - `http://localhost:8080/fhir/Patient/RPM-10001725`
   - `http://localhost:8080/fhir/Encounter?patient=RPM-10001725` (the admission you discharged)
   - `http://localhost:8080/fhir/Observation?patient=RPM-10001725&category=laboratory` (labs from the data, plus yours)
   - `http://localhost:8080/fhir/Flag?patient=RPM-10001725` (the alert)
   - `http://localhost:8080/fhir/RiskAssessment?subject=Patient/RPM-10001725` (risk scores)
   - `http://localhost:8080/fhir/DocumentReference?subject=Patient/RPM-10001725` (discharge summary, agents' summary)
3. **Audit trail.** Click **Audit trail** in the top bar: your reads and actions under your user name, each agent's
   reads under its own name (`agent:monitoring`, `agent:risk`, `agent:escalation`, `agent:summary`).
4. **Only training patients.** `make cohort` and the worklist list the same 30 ids, all starting `RPM-` or `SYN-`.
   `RPM_COHORT_HOME=0 make seed sim` monitors all 263.
5. **A real watch** in place of a simulated wearable: [watch-setup.md](watch-setup.md).

## Part E: the same test on AWS

```bash
aws login --profile rpm-capstone
make cloud-status          # prints the console address (https://….cloudfront.net)
make cloud-e2e             # Part A against AWS, signed in as a throw-away account
```

Then repeat Parts B to D in the browser at the console address. What differs:

| Local | On AWS |
|---|---|
| Sign in with the **Demo lead** button | Sign in with your own account (`make cloud-user U=… E=… G="physician presenter admin"`; the temporary password arrives by e-mail) |
| `make send …`, `make scenario …` | `make sim-cloud ARGS="send --target SYN-000160 --event SOS"`, `make sim-cloud ARGS="scenario --target RPM-10001725 --type sepsis --ramp-s 60"` (or the console's demo controls) |
| FHIR links at `localhost:8080` | `make cloud-fhir Q='Flag?patient=RPM-10001725'` (the FHIR server is not public) |
| E-mail link opens `localhost` | E-mail link opens the public console |
| Escalation every minute (local inbox) | Escalation every 5 minutes |
| `make logs`, `make ps` | `make cloud-logs S=care-api`, `make cloud-status` |

When you have finished: `make cloud-stop` (or `make cloud-sim-pause` to keep the console up without new alerts).

## If something does not happen

| Symptom | Check |
|---|---|
| No patients streaming | `make ps` (is `simulator` up?), then `make logs` |
| No alert after a scenario | Give it the full ramp time plus 30 seconds |
| "The agents are working on it…" for minutes | `make logs`; an expired AWS sign-in falls back to offline rules, a stopped container does not |
| Summary says "offline-rules" | Started without `LLM=bedrock`, or the AWS sign-in has expired (12 hours): sign in and `make up LLM=bedrock MAIL=ses` |
| No e-mail in your mailbox | `make mail-status` (is the address verified?), the Junk folder, and the alert's Notifications table |
| E-mail shows "not delivered: … not verified" | That teammate has not clicked the Amazon SES verification link yet |
| Lab or discharge does nothing | `make seed` creates the FHIR subscriptions; rerun it after a `make reset` |
| Too many alerts for a demonstration | `RPM_COHORT_HOME=8 make reset up seed sim` monitors fewer patients |
