-- 0005_offload_requests.sql: Offload requests table for Grok Bot box webhook

CREATE TABLE IF NOT EXISTS offload_requests (
    request_id          TEXT PRIMARY KEY,
    type                TEXT NOT NULL,   -- 'admin'|'video'
    chat_id             INTEGER,
    message_id          INTEGER,
    reply_to_message_id INTEGER,
    author_slug         TEXT,
    payload             TEXT,            -- JSON (directive, referenced_text, recent_messages)
    status              TEXT NOT NULL DEFAULT 'sent',  -- sent|acked|done|failed|expired
    commit_sha          TEXT,
    attempts            INTEGER DEFAULT 0,
    expires_at          TIMESTAMP,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_offload_requests_status ON offload_requests(status);
