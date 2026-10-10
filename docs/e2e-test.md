# End-to-end test script

Follow this top to bottom to see every part of the platform work. It takes about 20 minutes. Part A is automated;
Part B is the same journey by hand in the console, which is what you would show in a demonstration.

The steps are written for the local stack. On AWS the console address, the sign-in and the e-mail inbox differ
(see [runbook.md](runbook.md)); everything else is the same.

## Before you start

```bash
make setup && make models       # once
make up LLM=bedrock             # or plain `make up` for the offline agents (no AWS sign-in needed)
make seed
make sim
```

`make up LLM=bedrock` needs a current AWS sign-in: run `aws login --profile rpm-capstone` first. Wait about a
minute after `make sim` so every patient has data.

Add `MAIL=ses` to deliver alert e-mail to the care team's real mailboxes instead of the local inbox (see "Alert
e-mail to real inboxes" in the [runbook](runbook.md)). The automated check then needs
`make e2e ARGS='--mailpit ""'`.

## Part A: automated check

```bash
make e2e
```

It prints one PASS or FAIL line per check and stops at the first failure. A full run takes about four minutes and
covers live data, risk scores, a deterioration that pages the care team, the agents' cited summary, FHIR
write-back, messaging, a lab result arriving through the hospital record, a discharge, the audit trail and latency.

It uses up one ICU patient for the scenario and discharges another. With six ICU patients you can run it three
times; `make reset up seed sim` gives you a fresh cohort.

## Part B: by hand in the console

Open <http://localhost:3000> and sign in as **Demo lead** (clinical actions, demo controls and the audit trail in
one account). Keep <http://localhost:8025> (alert e-mail) open in a second tab.

### 1. Live monitoring

1. The worklist shows 24 patients: 6 in ICU, 18 at home. Rows flash as readings arrive.
2. Filter **ICU**, open a patient. The chart shows heart rate, SpO2, respiratory rate, blood pressure and
   temperature, updating every 10 seconds. The device is `sim-icu-…`.
3. Go back, filter **Post-discharge**, open a patient. Only heart rate, SpO2 and activity are measured, every 30
   seconds, and NEWS2 is marked partial (`*`). The device is `sim-…`.

Expected: an ICU patient never shows wearable data and a home patient never shows ICU monitor data.

### 2. Risk models

On any patient page, the **Risk models** card shows the models that apply: early deterioration for everyone,
readmission risk and recovery monitoring for patients at home. Each shows a probability, a band and the factors
that moved it. They are labelled "placeholder, not validated" until the team's models replace them.

### 3. Deterioration, alert and page

1. Open an ICU patient with no alert. In **Demo controls** choose `sepsis`, ramp 60, **Start scenario**.
2. Watch the chart: heart rate and respiratory rate climb, blood pressure falls, NEWS2 rises.
3. Within about a minute an **Active alert** appears on the page and in the worklist's alert inbox.
4. The alert e-mail arrives: in the local inbox at <http://localhost:8025>, or with `MAIL=ses` in the mailboxes of
   the people on the paged tier. It names the patient only by initials and the last four characters of the id, with
   no clinical details.
5. In the alert's **Notifications** table, "Cause → sent" shows the time from the sensor reading to the page. It
   should be well under a second.

Expected: the page goes out before the agents have produced anything.

### 4. The agents

1. On the same alert, **Agent summary (SBAR)** fills in after about 45 seconds (immediately in offline mode).
2. Click a citation such as `P-NEWS2#2`: the exact protocol passage opens underneath.
3. The badge reads "citations passed" when every reference was checked against what was retrieved.
4. **Agent assessment** shows the concern level and signal quality. **Timeline** shows what the agents did, for
   example raising the sampling rate or escalating to the next tier.

Expected: the agents may have raised the alert's severity; they never lower it.

### 5. Acting on the alert

1. Leave the alert unacknowledged for a minute: the "escalates in" countdown reaches zero, the tier rises and a
   second e-mail goes to the next tier. (Timers are shortened to one minute locally.)
2. Click **Acknowledge**. The countdown stops.
3. Click **Resolve**, give a reason, and the alert closes. **End** the scenario in Demo controls.

### 6. Talking to a patient at home

1. Open a home patient. In **Messages with the patient** type a short message and **Send**.
2. In **Actions** click **Ask: short of breath?**
3. Within about 20 seconds the simulated patient's answer appears in the message list.

A real watch shows the message and the question on screen; see [watch-setup.md](watch-setup.md).

### 7. A lab result from the hospital record

1. On a patient page, in **Demo controls**, leave Lactate 4.2 mmol/L, flag H, and click **File lab result**.
2. Within a few seconds it appears under **Lab results** and as a `LAB_RESULT` entry under **Clinical events**.

What happened: the console wrote a FHIR Observation to the hospital record, the FHIR server notified the platform,
and the result travelled the same queue as device data.

### 8. Discharge

1. Open an ICU patient with no alert. In **Demo controls** click **Discharge home**.
2. Within a few seconds the header changes from ICU to "home, discharged today" and the device changes from
   `sim-icu-…` to `sim-…`.
3. The chart continues with wearable data only. Blood pressure, respiratory rate and temperature stop.
4. **Risk models** now shows **Readmission risk** and, after a minute or two, **Recovery monitoring**.
5. With the agents on, a discharge summary appears under **Latest agent summary** after about a minute.

**Admit to ICU** on a home patient does the reverse.

### 9. The hospital record (FHIR)

Open <http://localhost:8080> and search, or use these addresses with a patient id from the console:

- `http://localhost:8080/fhir/Flag?patient=p-90000132` — the alert
- `http://localhost:8080/fhir/RiskAssessment?subject=Patient/p-90000132` — the risk scores
- `http://localhost:8080/fhir/DocumentReference?subject=Patient/p-90000132` — the agents' summary
- `http://localhost:8080/fhir/Encounter?patient=p-90000132` — the admission you discharged

### 10. Audit trail

Click **Audit trail** in the top bar. Your own reads and actions are listed with your user name, and each agent's
reads appear under its own name (`agent:monitoring`, `agent:risk`, `agent:escalation`, `agent:summary`).

### 11. One reading from the command line

```bash
make cohort                                        # pick a home patient id
make send P=p-90000019 ARGS="--hr 150 --spo2 84"   # one reading, sent right now
make send P=p-90000019 ARGS="--event SOS"          # the patient presses SOS
```

The SOS opens a critical alert at once. The same command works against AWS (see the runbook).

## If something does not happen

| Symptom | Check |
|---|---|
| No patients streaming | `make ps` (is `simulator` up?), then `make logs` |
| No alert after a scenario | Give it the full ramp time plus 30 seconds; home scenarios wait for the watch's next SpO2 reading |
| "The agents are working on it…" for minutes | `docker compose -f deploy/compose/docker-compose.yml logs agent-service`; an expired AWS sign-in falls back to offline rules, a stopped container does not |
| Summary says "offline-rules" | The stack was started without `LLM=bedrock`, or the AWS sign-in has expired (12 hours) |
| Lab or discharge does nothing | `make seed` creates the FHIR subscriptions; rerun it after a `make reset` |
