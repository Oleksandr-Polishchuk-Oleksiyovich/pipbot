-- 0004_video_checks.sql: Video checks queue and results table

CREATE TABLE IF NOT EXISTS video_checks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id       TEXT UNIQUE,
    chat_id          INTEGER NOT NULL,
    message_id       INTEGER NOT NULL,
    user_slug        TEXT,
    file_id          TEXT,
    file_unique_id   TEXT,
    duration_s       REAL,
    exercise         TEXT,            -- 'pushup'|'squat'|'none'
    reps             INTEGER,
    confidence       REAL,
    flags            TEXT,            -- JSON array
    valid            INTEGER DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|failed
    analyzer_version TEXT,
    location         TEXT,            -- 'vm'|'box'
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at      TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_video_checks_status ON video_checks(status);
CREATE INDEX IF NOT EXISTS idx_video_checks_file_unique_id ON video_checks(file_unique_id);
