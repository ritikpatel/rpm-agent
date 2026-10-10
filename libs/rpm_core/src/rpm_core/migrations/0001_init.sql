-- Operational store for the RPM platform (single baseline migration). FHIR (HAPI) remains the clinical system of record;
-- these tables hold the device registry, high-frequency vitals and pipeline state.

CREATE EXTENSION IF NOT EXISTS vector;   -- used by the guideline index

-- ---------------------------------------------------------------- patients & device registry
CREATE TABLE IF NOT EXISTS patients (
    patient_id                text PRIMARY KEY,           -- also the FHIR Patient.id
    subject_id                text,
    display_name              text NOT NULL,
    sex                       text,
    age                       int,
    primary_condition         text,                       -- category used for thresholds (Sepsis, COPD, ...)
    primary_diagnosis         text,                       -- diagnosis text from the record, when there is one
    data_origin               text,                       -- mimic_real | synthetic (provenance in the training data)
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
    device_kind  text NOT NULL CHECK (device_kind IN ('watch', 'icu_monitor', 'simulator')),
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


-- alert lifecycle + escalation, notifications, agent runs, risk scores, RAG index.

-- ---------------------------------------------------------------- richer patient context (model features, escalation)
ALTER TABLE patients
    ADD COLUMN IF NOT EXISTS los_days real,
    ADD COLUMN IF NOT EXISTS prior_admissions_12m int,
    ADD COLUMN IF NOT EXISTS discharge_disposition text,
    ADD COLUMN IF NOT EXISTS drg_severity int,
    ADD COLUMN IF NOT EXISTS care_team jsonb NOT NULL DEFAULT '{}'::jsonb;  -- per-patient escalation contacts

-- ---------------------------------------------------------------- alerts
-- One active alert per patient: new events attach to it (and can raise its severity) instead of paging again.
CREATE TABLE IF NOT EXISTS alerts (
    alert_id            uuid PRIMARY KEY,
    patient_id          text NOT NULL REFERENCES patients(patient_id),
    status              text NOT NULL CHECK (status IN ('open', 'acknowledged', 'escalated', 'resolved')),
    severity            text NOT NULL CHECK (severity IN ('medium', 'high', 'critical')),
    tier                smallint NOT NULL,                 -- current escalation tier (see escalation policy)
    title               text NOT NULL,
    source_event_id     uuid NOT NULL,
    event_types         text[] NOT NULL DEFAULT '{}',
    event_count         int NOT NULL DEFAULT 1,
    opened_at           timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    first_event_at      timestamptz NOT NULL,              -- sensor time of the first event (latency baseline)
    acknowledged_at     timestamptz,
    acknowledged_by     text,
    resolved_at         timestamptz,
    resolved_by         text,
    resolution          text,
    next_escalation_at  timestamptz,
    brief               jsonb,                             -- SBAR brief with citations (agent output)
    brief_status        text NOT NULL DEFAULT 'pending' CHECK (brief_status IN ('pending', 'ready', 'failed', 'offline')),
    agent_assessment    jsonb,
    notify_pending_tier smallint,                          -- outbox: set in the same transaction as the state change
    reminders_sent      int NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS alerts_one_active_per_patient
    ON alerts (patient_id) WHERE status IN ('open', 'acknowledged', 'escalated');
CREATE INDEX IF NOT EXISTS alerts_due ON alerts (next_escalation_at) WHERE status IN ('open', 'escalated');
CREATE INDEX IF NOT EXISTS alerts_recent ON alerts (opened_at DESC);
CREATE INDEX IF NOT EXISTS alerts_notify_pending ON alerts (updated_at) WHERE notify_pending_tier IS NOT NULL;

-- exactly-once effect for at-least-once queues: recorded in the same transaction as the consumer's writes
CREATE TABLE IF NOT EXISTS consumed_events (
    consumer     text NOT NULL,
    event_id     uuid NOT NULL,
    consumed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer, event_id)
);

CREATE TABLE IF NOT EXISTS alert_timeline (
    id          bigserial PRIMARY KEY,
    alert_id    uuid NOT NULL REFERENCES alerts(alert_id),
    at          timestamptz NOT NULL DEFAULT now(),
    kind        text NOT NULL,     -- opened | event_attached | severity_raised | notified | escalated | acknowledged |
                                   -- resolved | agent_assessment | agent_action | agent_brief | patient_reply | note
    actor       text NOT NULL,     -- system | agent:<name> | user:<id> | patient
    summary     text NOT NULL,
    details     jsonb
);
CREATE INDEX IF NOT EXISTS alert_timeline_alert ON alert_timeline (alert_id, at);

CREATE TABLE IF NOT EXISTS notifications (
    notification_id  uuid PRIMARY KEY,
    alert_id         uuid NOT NULL REFERENCES alerts(alert_id),
    tier             smallint NOT NULL,
    channel          text NOT NULL,          -- console | email | sms | watch | webhook
    recipient        text NOT NULL,
    status           text NOT NULL CHECK (status IN ('sent', 'failed', 'skipped')),
    sent_at          timestamptz NOT NULL DEFAULT now(),
    latency_ms       int,                    -- from the first event's sensor time to hand-off to the channel
    provider_ref     text,
    error            text,
    payload          jsonb
);
CREATE INDEX IF NOT EXISTS notifications_alert ON notifications (alert_id, sent_at);

-- ---------------------------------------------------------------- agents
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id         uuid PRIMARY KEY,
    event_id       uuid,
    patient_id     text NOT NULL,
    alert_id       uuid,
    trigger        text NOT NULL,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    status         text NOT NULL CHECK (status IN ('running', 'succeeded', 'degraded', 'failed', 'skipped')),
    provider       text,
    model          text,
    input_tokens   int,
    output_tokens  int,
    cache_read_tokens int,
    assessment     jsonb,
    actions        jsonb,
    brief          jsonb,
    trace          jsonb,                  -- per-node timings, tool calls, retrieval ids (audit)
    error          text
);
CREATE INDEX IF NOT EXISTS agent_runs_patient ON agent_runs (patient_id, started_at DESC);

-- ---------------------------------------------------------------- risk models
CREATE TABLE IF NOT EXISTS risk_scores (
    id             bigserial PRIMARY KEY,
    patient_id     text NOT NULL,
    model          text NOT NULL,
    model_version  text NOT NULL,
    probability    real NOT NULL,
    band           text NOT NULL,
    top_factors    jsonb NOT NULL,
    features       jsonb NOT NULL,
    computed_at    timestamptz NOT NULL DEFAULT now(),
    fhir_id        text
);
CREATE INDEX IF NOT EXISTS risk_scores_latest ON risk_scores (patient_id, model, computed_at DESC);

-- ---------------------------------------------------------------- RAG index (hybrid: full-text + pgvector)
CREATE TABLE IF NOT EXISTS rag_documents (
    doc_id      text PRIMARY KEY,
    kind        text NOT NULL CHECK (kind IN ('protocol', 'discharge_summary')),
    title       text NOT NULL,
    source      text NOT NULL,
    license     text NOT NULL,
    patient_id  text,                      -- set for patient-specific documents only
    content     text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rag_chunks (
    chunk_id         text PRIMARY KEY,
    doc_id           text NOT NULL REFERENCES rag_documents(doc_id) ON DELETE CASCADE,
    ord              int NOT NULL,
    heading          text,
    text             text NOT NULL,
    patient_id       text,
    tsv              tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(heading, '') || ' ' || text)) STORED,
    embedding        vector(1024),
    embedding_model  text
);
CREATE INDEX IF NOT EXISTS rag_chunks_tsv ON rag_chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS rag_chunks_patient ON rag_chunks (patient_id);
CREATE INDEX IF NOT EXISTS rag_chunks_embedding ON rag_chunks USING hnsw (embedding vector_cosine_ops);


-- measure each page from what caused it (a sensor event, an escalation timer, the agent or a clinician)
-- rather than from the alert's first event, so a late SOS on an old alert reads 1 s, not 2 minutes.
ALTER TABLE alerts
    ADD COLUMN IF NOT EXISTS notify_cause text,             -- event | timer | agent | manual (pending page only)
    ADD COLUMN IF NOT EXISTS notify_cause_at timestamptz;   -- sensor time / timer due time / request time
ALTER TABLE notifications
    ADD COLUMN IF NOT EXISTS cause text;


-- audit_log is append-only at the database level, and exports to the write-once bucket are hash-chained.
-- (GDPR erasure does not apply to audit records kept under a legal obligation: Art. 17(3)(b).)
CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only (% refused)', TG_OP;
END $$;
DROP TRIGGER IF EXISTS audit_log_no_change ON audit_log;
CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();
DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_immutable();

CREATE TABLE IF NOT EXISTS audit_exports (
    batch_id     bigserial PRIMARY KEY,
    first_id     bigint NOT NULL,
    last_id      bigint NOT NULL,
    row_count    int NOT NULL,
    object_key   text NOT NULL,
    sha256       text NOT NULL,          -- of the exported object's bytes
    prev_sha256  text,                   -- chain: a removed or altered batch breaks it
    exported_at  timestamptz NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------- stream processor state (survives restarts)
CREATE TABLE IF NOT EXISTS stream_state (
    patient_id   text PRIMARY KEY,
    rule_state   text NOT NULL,            -- RuleState JSON (baseline history, sustained-condition timers)
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- lab results (from the EHR over FHIR)
CREATE TABLE IF NOT EXISTS labs (
    id          bigserial PRIMARY KEY,
    patient_id  text NOT NULL,
    ts          timestamptz NOT NULL,
    code        text NOT NULL,             -- LOINC or local item code
    name        text NOT NULL,
    value       real,
    unit        text,
    flag        text,                      -- abnormal flag from the lab system, if any
    fhir_id     text UNIQUE
);
CREATE INDEX IF NOT EXISTS labs_patient ON labs (patient_id, ts DESC);

-- ---------------------------------------------------------------- EHR events consumed from the unified stream
CREATE TABLE IF NOT EXISTS ehr_events (
    event_id     text PRIMARY KEY,
    patient_id   text NOT NULL,
    type         text NOT NULL CHECK (type IN ('admission', 'discharge', 'lab')),
    occurred_at  timestamptz NOT NULL,
    payload      jsonb NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ehr_events_patient ON ehr_events (patient_id, occurred_at DESC);

-- ---------------------------------------------------------------- clinician <-> patient messages (watch)
CREATE TABLE IF NOT EXISTS messages (
    id          bigserial PRIMARY KEY,
    patient_id  text NOT NULL REFERENCES patients(patient_id),
    direction   text NOT NULL CHECK (direction IN ('to_patient', 'from_patient')),
    kind        text NOT NULL,             -- message | checkin | reply | sos
    text        text NOT NULL,
    sender      text NOT NULL,             -- user:<name> | agent:<name> | patient
    device_id   text,
    cmd_id      uuid,
    at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_patient ON messages (patient_id, at DESC);

-- ---------------------------------------------------------------- FHIR write-back bookkeeping
ALTER TABLE alerts
    ADD COLUMN IF NOT EXISTS fhir_flag_id  text,     -- Flag created for this alert
    ADD COLUMN IF NOT EXISTS fhir_brief_id text;     -- DocumentReference holding the agent's SBAR summary
