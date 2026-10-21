"""
0010_admin_tasks.py: черга адмін-запитів, які не покриває жоден вузький адмін-тул (create_admin_task).
Розробник (box) забирає їх через gateway (/admin_tasks/claim), після фіксу позначає done_pending_deploy
з fix_commit; бот після деплою (на старті) анонсує «Пропатчився…». Сідить дефолтні bot_texts (INSERT OR IGNORE).
"""
import sqlite3

from prompts import DEFAULT_BOT_TEXTS

ADMIN_TASK_TEXT_KEYS = ("admin_task_created", "admin_task_patched", "admin_task_rejected")


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_tasks (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id               INTEGER,
            requester_telegram_id INTEGER,
            requester_name        TEXT,
            text                  TEXT NOT NULL,
            status                TEXT NOT NULL DEFAULT 'pending'
                                  CHECK (status IN ('pending','claimed','done_pending_deploy','announced','rejected')),
            fix_commit            TEXT,
            result                TEXT,
            notified              INTEGER NOT NULL DEFAULT 0,
            created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_tasks_status ON admin_tasks(status)")
    has_texts = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bot_texts'").fetchone()
    if has_texts:
        for key in ADMIN_TASK_TEXT_KEYS:
            conn.execute(
                "INSERT OR IGNORE INTO bot_texts (key, text, updated_by, updated_at) VALUES (?, ?, 'seed', datetime('now'))",
                (key, DEFAULT_BOT_TEXTS[key]),
            )
