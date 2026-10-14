-- What the team's models read about a patient besides the live readings (written by `rpm-seed registry`):
--   record_facts     admission-level facts from the hospital record (labs and vital signs charted before discharge,
--                    length of stay, comorbidities, medicines)
--   last_recorded    activity, steps and patient-reported values at the last recorded snapshot (not streamed)
--   discharge_vitals vital signs at discharge, the baseline the models compare the current readings with
ALTER TABLE patients ADD COLUMN IF NOT EXISTS record_facts jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE patients ADD COLUMN IF NOT EXISTS last_recorded jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE patients ADD COLUMN IF NOT EXISTS discharge_vitals jsonb NOT NULL DEFAULT '{}'::jsonb;
