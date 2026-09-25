-- Week 3: measure each page from what caused it (a sensor event, an escalation timer, the agent or a clinician)
-- rather than from the alert's first event, so a late SOS on an old alert reads 1 s, not 2 minutes.
ALTER TABLE alerts
    ADD COLUMN IF NOT EXISTS notify_cause text,             -- event | timer | agent | manual (pending page only)
    ADD COLUMN IF NOT EXISTS notify_cause_at timestamptz;   -- sensor time / timer due time / request time
ALTER TABLE notifications
    ADD COLUMN IF NOT EXISTS cause text;
