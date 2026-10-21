"""
0008_bot_texts.py: Таблиця bot_texts — фіксовані відповіді бота/тон-зразки з адмінською правкою
без деплою (core/bot_texts.py BotTexts читає БД на кожен виклик, інакше дефолт із коду).

Ідемпотентно сідить дефолти з prompts.DEFAULT_BOT_TEXTS (INSERT OR IGNORE — ніколи не
перезаписує вже існуючий рядок, у т.ч. адмінську правку). Легасі ключі bot_state 'text:<key>'
(інтерімний lookup до появи цієї таблиці) переносяться в bot_texts під ключем без префікса
'text:' (перезаписуючи сід для цього ключа — легасі значення має пріоритет) і видаляються з bot_state.
"""
import sqlite3

from prompts import DEFAULT_BOT_TEXTS


def upgrade(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_texts (
            key         TEXT PRIMARY KEY,
            text        TEXT NOT NULL,
            variants    TEXT,
            updated_by  TEXT,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    for key, text in DEFAULT_BOT_TEXTS.items():
        conn.execute(
            "INSERT OR IGNORE INTO bot_texts (key, text, updated_by, updated_at) "
            "VALUES (?, ?, 'seed', datetime('now'))",
            (key, text),
        )

    has_bot_state = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bot_state'"
    ).fetchone()
    if not has_bot_state:
        return

    legacy_rows = conn.execute("SELECT key, value FROM bot_state WHERE key LIKE 'text:%'").fetchall()
    for state_key, value in legacy_rows:
        bot_text_key = state_key[len("text:"):]
        if not bot_text_key or value is None:
            continue
        conn.execute("""
            INSERT INTO bot_texts (key, text, updated_by, updated_at)
            VALUES (?, ?, 'bot_state_migration', datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                text = excluded.text,
                updated_by = excluded.updated_by,
                updated_at = datetime('now')
        """, (bot_text_key, value))
        conn.execute("DELETE FROM bot_state WHERE key = ?", (state_key,))
