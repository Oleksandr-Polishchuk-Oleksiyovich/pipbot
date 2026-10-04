"""
Синхронне сховище бота поверх SQLite: прогрес, історія чату, bot_state та версіоновані промпти агентів.
Кожен виклик відкриває власне з'єднання — безпечно для asyncio.to_thread та для тулів Gemini.
"""
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class ChatMessage:
    id: int
    user_name: str
    role: str
    text: str
    created_at: str

    def render(self) -> str:
        sender = self.user_name if self.role == "user" else "Тренер (Спорт Зануда)"
        return f"[{self.created_at}] {sender}: {self.text}"


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    # --- СХЕМА ---
    def init_schema(self) -> None:
        with self._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS progress (
                    date TEXT PRIMARY KEY,
                    lyosha_done INTEGER DEFAULT 0,
                    sasha_done INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS chat_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    user_name TEXT,
                    role TEXT,
                    text TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS prompts (
                    key TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_by TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS prompt_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL,
                    content TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    updated_by TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

    # --- BOT STATE ---
    def get_state(self, key: str, default: str = "") -> str:
        with self._connect() as con:
            row = con.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row and row[0] is not None else default

    def set_state(self, key: str, value) -> None:
        with self._connect() as con:
            con.execute("""
                INSERT INTO bot_state (key, value, updated_at) VALUES (?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')
            """, (key, str(value)))

    def all_state(self) -> Dict[str, str]:
        with self._connect() as con:
            return dict(con.execute("SELECT key, value FROM bot_state").fetchall())

    # --- PROGRESS ---
    def get_progress(self, date_iso: str) -> Tuple[bool, bool]:
        """Повертає (lyosha_done, sasha_done)."""
        with self._connect() as con:
            row = con.execute(
                "SELECT lyosha_done, sasha_done FROM progress WHERE date = ?", (date_iso,)
            ).fetchone()
        return (bool(row[0]), bool(row[1])) if row else (False, False)

    def set_done(self, participant: str, done: bool, date_iso: str) -> None:
        if participant not in ("lyosha", "sasha"):
            raise ValueError(f"Unknown participant: {participant}")
        column = f"{participant}_done"
        with self._connect() as con:
            con.execute("INSERT OR IGNORE INTO progress (date) VALUES (?)", (date_iso,))
            con.execute(f"UPDATE progress SET {column} = ? WHERE date = ?", (1 if done else 0, date_iso))

    # --- CHAT HISTORY ---
    def save_message(self, chat_id: int, user_name: str, role: str, text: str, created_at: str) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT INTO chat_history (chat_id, user_name, role, text, created_at) VALUES (?, ?, ?, ?, ?)",
                (chat_id, user_name, role, text, created_at),
            )

    def unprocessed_user_messages(self, chat_id: int, after_ts: str, day_start_ts: str) -> List[ChatMessage]:
        with self._connect() as con:
            rows = con.execute("""
                SELECT id, user_name, role, text, created_at FROM chat_history
                WHERE chat_id = ? AND role = 'user' AND created_at > ? AND created_at >= ?
                ORDER BY id ASC
            """, (chat_id, after_ts, day_start_ts)).fetchall()
        return [ChatMessage(*r) for r in rows]

    def messages_since(self, chat_id: int, start_ts: str, limit: int = 200) -> List[ChatMessage]:
        with self._connect() as con:
            rows = con.execute("""
                SELECT id, user_name, role, text, created_at FROM chat_history
                WHERE chat_id = ? AND created_at >= ?
                ORDER BY id DESC LIMIT ?
            """, (chat_id, start_ts, limit)).fetchall()
        return [ChatMessage(*r) for r in reversed(rows)]

    # --- PROMPTS ---
    def get_prompt(self, key: str) -> Optional[str]:
        with self._connect() as con:
            row = con.execute("SELECT content FROM prompts WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def get_prompt_version(self, key: str) -> int:
        with self._connect() as con:
            row = con.execute("SELECT version FROM prompts WHERE key = ?", (key,)).fetchone()
        return row[0] if row else 0

    def save_prompt(self, key: str, content: str, updated_by: str) -> int:
        """Зберігає нову версію промпту та архівує її в prompt_history. Повертає номер версії."""
        with self._connect() as con:
            row = con.execute("SELECT version FROM prompts WHERE key = ?", (key,)).fetchone()
            version = (row[0] + 1) if row else 1
            con.execute("""
                INSERT INTO prompts (key, content, version, updated_by, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET content = excluded.content, version = excluded.version,
                    updated_by = excluded.updated_by, updated_at = datetime('now')
            """, (key, content, version, updated_by))
            con.execute(
                "INSERT INTO prompt_history (key, content, version, updated_by) VALUES (?, ?, ?, ?)",
                (key, content, version, updated_by),
            )
        logger.info("Prompt [%s] saved as v%d by %s (%d chars)", key, version, updated_by, len(content))
        return version

    def archive_prompt(self, key: str, content: str, version: int, updated_by: str) -> None:
        """Пише лише в prompt_history (без зміни активного промпту) — для збереження легасі-версій."""
        with self._connect() as con:
            con.execute(
                "INSERT INTO prompt_history (key, content, version, updated_by) VALUES (?, ?, ?, ?)",
                (key, content, version, updated_by),
            )

    def prompt_history(self, key: str) -> List[Tuple[int, str, str]]:
        with self._connect() as con:
            return con.execute(
                "SELECT version, updated_by, content FROM prompt_history WHERE key = ? ORDER BY id",
                (key,),
            ).fetchall()

    def seed_prompts(self, defaults: Dict[str, str]) -> List[str]:
        """
        Додає відсутні промпти та оновлює ті, що досі належать seed (новий дефолт із коду).
        Промпти, змінені адміном (updated_by != 'seed'), не чіпає.
        """
        seeded = []
        for key, content in defaults.items():
            with self._connect() as con:
                row = con.execute("SELECT content, updated_by FROM prompts WHERE key = ?", (key,)).fetchone()
            if row is None or (row[1] == "seed" and row[0] != content):
                self.save_prompt(key, content, "seed")
                seeded.append(key)
        return seeded


def format_ts(dt: datetime) -> str:
    return dt.strftime(TS_FORMAT)
