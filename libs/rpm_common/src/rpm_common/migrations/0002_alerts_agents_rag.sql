-- Week 2: alert lifecycle + escalation, notifications, agent runs, risk scores, RAG index.

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
