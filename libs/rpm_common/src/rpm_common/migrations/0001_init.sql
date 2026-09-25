-- Operational store for the RPM platform. FHIR (HAPI) remains the clinical system of record;
-- these tables hold the device registry, high-frequency vitals and pipeline state.

CREATE EXTENSION IF NOT EXISTS vector;   -- used by the RAG index (week 2)

-- ---------------------------------------------------------------- patients & device registry
CREATE TABLE IF NOT EXISTS patients (
    patient_id                text PRIMARY KEY,           -- also the FHIR Patient.id
    subject_id                text,
    display_name              text NOT NULL,
    sex                       text,
    age                       int,
    primary_condition         text,
    care_setting              text NOT NULL CHECK (care_setting IN ('home', 'icu')),
    discharge_at              timestamptz,
    icu_admit_at              timestamptz,
    hypercapnic_resp_failure  boolean NOT NULL DEFAULT false,   -- selects NEWS2 SpO2 scale 2
    on_supplemental_o2        boolean NOT NULL DEFAULT false,
    thresholds                jsonb NOT NULL DEFAULT '{}'::jsonb, -- per-patient overrides (mirrors device shadow)
    monitoring_active         boolean NOT NULL DEFAULT true,
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS device_assignments (
    device_id    text NOT NULL,                              -- == AWS IoT thing name
    patient_id   text NOT NULL REFERENCES patients(patient_id),
    device_kind  text NOT NULL CHECK (device_kind IN ('watch', 'bedside', 'simulator')),
    active_from  timestamptz NOT NULL DEFAULT now(),
    active_to    timestamptz,
    PRIMARY KEY (device_id, active_from)
);
CREATE UNIQUE INDEX IF NOT EXISTS device_assignments_one_active
    ON device_assignments (device_id) WHERE active_to IS NULL;
CREATE INDEX IF NOT EXISTS device_assignments_patient
    ON device_assignments (patient_id) WHERE active_to IS NULL;

-- ---------------------------------------------------------------- vitals (daily partitions)
CREATE TABLE IF NOT EXISTS vitals (
    patient_id      text NOT NULL,
    device_id       text NOT NULL,
    ts              timestamptz NOT NULL,                    -- window_end (event time)
    msg_id          uuid NOT NULL,
    source          text NOT NULL,
    context         text NOT NULL,
    heart_rate      real, hr_min real, hr_max real, hrv_rmssd real,
    spo2            real, resp_rate real, sbp real, dbp real,
    temp            real, skin_temp real, gcs real, steps real,
    activity        text,
    quality         jsonb,
    news2           smallint,
    news2_band      text,
    news2_partial   boolean,
    edge_flags      text[],
    buffered        boolean NOT NULL DEFAULT false,
    late            boolean NOT NULL DEFAULT false,
    edge_sent       timestamptz,
    ingest_at       timestamptz,
    processed_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (msg_id, ts)
) PARTITION BY RANGE (ts);

CREATE INDEX IF NOT EXISTS vitals_patient_ts ON vitals (patient_id, ts DESC);
CREATE TABLE IF NOT EXISTS vitals_default PARTITION OF vitals DEFAULT;

CREATE OR REPLACE FUNCTION ensure_vitals_partitions(days_back int DEFAULT 2, days_ahead int DEFAULT 7)
RETURNS int LANGUAGE plpgsql AS $$
DECLARE
    d date;
    part text;
    created int := 0;
BEGIN
    FOR d IN SELECT generate_series(current_date - days_back, current_date + days_ahead, interval '1 day')::date LOOP
        part := format('vitals_%s', to_char(d, 'YYYYMMDD'));
        IF to_regclass(part) IS NULL THEN
            BEGIN
                EXECUTE format('CREATE TABLE %I PARTITION OF vitals FOR VALUES FROM (%L) TO (%L)', part, d, d + 1);
                created := created + 1;
            EXCEPTION WHEN others THEN
                RAISE WARNING 'could not create partition %: %', part, SQLERRM;
            END;
        END IF;
    END LOOP;
    RETURN created;
END $$;

SELECT ensure_vitals_partitions();

-- ---------------------------------------------------------------- current state per patient
CREATE TABLE IF NOT EXISTS patient_status (
    patient_id      text PRIMARY KEY REFERENCES patients(patient_id),
    device_id       text,
    last_seen       timestamptz,
    latest          jsonb NOT NULL DEFAULT '{}'::jsonb,
    news2           smallint,
    news2_band      text,
    news2_partial   boolean,
    news2_detail    jsonb,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- clinical events (rule output)
CREATE TABLE IF NOT EXISTS clinical_events (
    event_id        uuid PRIMARY KEY,
    patient_id      text NOT NULL,
    device_id       text,
    type            text NOT NULL,
    severity        text NOT NULL,
    requires_alert  boolean NOT NULL,
    occurred_at     timestamptz NOT NULL,
    detected_at     timestamptz NOT NULL,
    summary         text NOT NULL,
    payload         jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS clinical_events_patient ON clinical_events (patient_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS clinical_events_detected ON clinical_events (detected_at DESC);

-- ---------------------------------------------------------------- stream consumer checkpoints
CREATE TABLE IF NOT EXISTS stream_checkpoints (
    consumer         text NOT NULL,
    stream           text NOT NULL,
    shard_id         text NOT NULL,
    sequence_number  text NOT NULL,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer, stream, shard_id)
);

-- ---------------------------------------------------------------- PHI access audit (HIPAA 164.312(b))
CREATE TABLE IF NOT EXISTS audit_log (
    id              bigserial PRIMARY KEY,
    at              timestamptz NOT NULL DEFAULT now(),
    actor           text NOT NULL,
    actor_role      text,
    action          text NOT NULL,          -- read | create | update | delete | export
    resource_type   text NOT NULL,
    resource_id     text,
    patient_id      text,
    outcome         text NOT NULL DEFAULT 'success',
    source_ip       text,
    details         jsonb
);
CREATE INDEX IF NOT EXISTS audit_log_patient ON audit_log (patient_id, at DESC);
