-- Week 4: audit_log is append-only at the database level, and exports to the write-once bucket are hash-chained.
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
