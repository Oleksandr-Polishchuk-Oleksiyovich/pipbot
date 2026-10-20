-- 0006_admin_audit.sql: Admin dev audit trail and guardrails tracking

CREATE TABLE IF NOT EXISTS admin_audit (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id          TEXT NOT NULL,
    author_slug         TEXT,
    author_telegram_id  INTEGER,
    request             TEXT,
    plan                TEXT,
    commit_sha          TEXT,
    status              TEXT NOT NULL DEFAULT 'planned',  -- planned|waiting_approval|approved|pushed|deployed|failed|rejected
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_request_id ON admin_audit(request_id);
CREATE INDEX IF NOT EXISTS idx_admin_audit_status ON admin_audit(status);
