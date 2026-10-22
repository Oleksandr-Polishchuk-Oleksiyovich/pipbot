"""
0011_progression.py: адаптивна наступна норма + найкраща спроба дня + XP/рівні (core/progression.py).
Таблиці daily_attempts, user_xp, next_norms, xp_log; сідить нові bot_texts (INSERT OR IGNORE).
Бекап БД робить database/migrate.py перед накатом.
"""
import sqlite3

from prompts import DEFAULT_BOT_TEXTS

TEXT_KEYS = ("admin_dev_accepted", "admin_task_still_working", "level_up", "progress_settled", "status_xp")


def upgrade(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS daily_attempts (
            date        TEXT NOT NULL,
            user_slug   TEXT NOT NULL,
            exercise    TEXT,
            best_reps   INTEGER NOT NULL DEFAULT 0,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (date, user_slug)
        );
        CREATE TABLE IF NOT EXISTS user_xp (
            user_slug   TEXT PRIMARY KEY,
            xp          INTEGER NOT NULL DEFAULT 0,
            level       INTEGER NOT NULL DEFAULT 0,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS next_norms (
            user_slug      TEXT NOT NULL,
            exercise       TEXT NOT NULL,
            target         INTEGER NOT NULL,
            effective_from TEXT NOT NULL,
            source_date    TEXT,
            PRIMARY KEY (user_slug, exercise)
        );
        CREATE TABLE IF NOT EXISTS xp_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT,
            user_slug   TEXT,
            delta       INTEGER,
            reason      TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    has_texts = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bot_texts'").fetchone()
    if has_texts:
        for key in TEXT_KEYS:
            conn.execute(
                "INSERT OR IGNORE INTO bot_texts (key, text, updated_by, updated_at) VALUES (?, ?, 'seed', datetime('now'))",
                (key, DEFAULT_BOT_TEXTS[key]),
            )
