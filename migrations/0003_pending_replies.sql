-- 0003_pending_replies.sql: Table for replies that must survive bot restart

CREATE TABLE IF NOT EXISTS pending_replies (
    request_id          TEXT PRIMARY KEY,
    chat_id             INTEGER NOT NULL,
    reply_to_message_id INTEGER,
    text                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    attempts            INTEGER DEFAULT 0,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_pending_replies_status ON pending_replies(status);
