"""
0012_leaves.py: система «вакейшенів та ілнесів» (відпустка / лікарняний) для SportNerdBot.
Таблиця leaves, індекс (user_slug, start_date).
"""
import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS leaves (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_slug   TEXT NOT NULL,
            kind        TEXT NOT NULL,
            start_date  TEXT NOT NULL,
            end_date    TEXT,
            set_by      TEXT,
            reason      TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ended_at    TIMESTAMP,
            ended_by    TEXT,
            cancelled   INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_leaves_user_start ON leaves(user_slug, start_date);
    """)
