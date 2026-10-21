-- 0009_cv_jobs.sql: черга офлоаду CV-підрахунку на box worker (long-poll через gateway).
-- Один рядок на video_checks.request_id. Lease: claimed job без результату довше lease -> requeue.

CREATE TABLE IF NOT EXISTS cv_jobs (
    request_id        TEXT PRIMARY KEY,
    status            TEXT NOT NULL DEFAULT 'queued',   -- queued|claimed|done|failed
    claim_token       TEXT,
    claimed_at        TIMESTAMP,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    timings           TEXT,                              -- JSON від worker-а
    processing_sent_at TIMESTAMP,
    still_sent_at     TIMESTAMP,
    replied_at        TIMESTAMP,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cv_jobs_status ON cv_jobs(status);
