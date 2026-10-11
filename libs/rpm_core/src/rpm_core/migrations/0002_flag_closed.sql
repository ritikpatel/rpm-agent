-- A resolved alert's FHIR Flag is rewritten once as inactive, with the time it ended; this records that it has been.
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS fhir_flag_closed boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS alerts_flag_to_close ON alerts (resolved_at)
    WHERE status = 'resolved' AND fhir_flag_id IS NOT NULL AND NOT fhir_flag_closed;
