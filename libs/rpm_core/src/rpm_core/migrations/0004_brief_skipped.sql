-- An alert whose agent run was not started (the daily limit, or the queue unreachable) says so, instead of waiting
-- as "pending" for a summary that will never come. An on-demand review still replaces it with a summary.
ALTER TABLE alerts DROP CONSTRAINT IF EXISTS alerts_brief_status_check;
ALTER TABLE alerts ADD CONSTRAINT alerts_brief_status_check
    CHECK (brief_status IN ('pending', 'ready', 'failed', 'offline', 'skipped'));
