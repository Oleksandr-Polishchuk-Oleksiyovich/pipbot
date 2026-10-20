"""
Синхронне сховище бота поверх SQLite: користувачі (users), прогрес (user_progress),
історія чату (chat_history), bot_state та версіоновані промпти агентів (prompts / prompt_history).
Кожен виклик відкриває власне з'єднання — безпечно для asyncio.to_thread та для тулів Gemini.
"""
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TS_FORMAT = "%Y-%m-%d %H:%M:%S"

DIFFICULTY_LIVES = {
    "hardcore": 2,
    "normal": 5,
    "welcome": 10,
}

_CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ye",
    "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "yi", "й": "y", "к": "k", "л": "l",
    "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ь": "",
    "ю": "yu", "я": "ya", "ы": "y", "э": "e", "ё": "yo", "ъ": "",
}


def slugify(text: str, fallback: str = "user") -> str:
    raw = (text or "").lower().strip()
    out = []
    for ch in raw:
        if ch in _CYR_TO_LAT:
            out.append(_CYR_TO_LAT[ch])
        elif "a" <= ch <= "z" or "0" <= ch <= "9":
            out.append(ch)
        elif ch in (" ", "_", "-"):
            out.append("_")
    slug = re.sub(r"_+", "_", "".join(out)).strip("_")[:24]
    if not slug or not slug[0].isalpha():
        slug = f"{fallback}_{slug}" if slug else fallback
    return slug


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


@dataclass(frozen=True)
class PendingReply:
    request_id: str
    chat_id: int
    reply_to_message_id: Optional[int]
    text: str
    status: str = "pending"
    attempts: int = 0
    created_at: str = ""


@dataclass
class User:
    slug: str
    telegram_id: Optional[int] = None
    telegram_name: str = ""
    username: str = ""
    display_name: str = ""
    emoji: str = "🏋️"
    description: str = ""
    aliases: List[str] = field(default_factory=list)
    status: str = "uninitialized"  # 'uninitialized' | 'active' | 'inactive'
    difficulty: str = "normal"     # 'hardcore' (2) | 'normal' (5) | 'welcome' (10) | 'custom'
    lives: Optional[int] = None
    max_lives: Optional[int] = None
    base_pushups: int = 10
    ladder_offset: int = 0
    alt_pushups: Optional[int] = None
    max_pushups_reported: Optional[int] = None
    max_squats_reported: Optional[int] = None
    joined_date: str = "2026-08-31"

    @classmethod
    def from_row(cls, row: Tuple) -> "User":
        (
            slug, telegram_id, telegram_name, username, display_name, emoji,
            description, aliases_json, status, difficulty, lives, max_lives,
            base_pushups, ladder_offset, alt_pushups, max_pushups_reported,
            max_squats_reported, joined_date
        ) = row
        try:
            aliases = json.loads(aliases_json or "[]")
        except Exception:
            aliases = []
        return cls(
            slug=slug,
            telegram_id=telegram_id,
            telegram_name=telegram_name or "",
            username=username or "",
            display_name=display_name or telegram_name or slug,
            emoji=emoji or "🏋️",
            description=description or "",
            aliases=aliases,
            status=status or "uninitialized",
            difficulty=difficulty or "normal",
            lives=int(lives) if lives is not None else None,
            max_lives=int(max_lives) if max_lives is not None else None,
            base_pushups=int(base_pushups or 0),
            ladder_offset=int(ladder_offset or 0),
            alt_pushups=int(alt_pushups) if alt_pushups is not None else None,
            max_pushups_reported=int(max_pushups_reported) if max_pushups_reported is not None else None,
            max_squats_reported=int(max_squats_reported) if max_squats_reported is not None else None,
            joined_date=joined_date or "2026-08-31",
        )


_USER_COLS = (
    "slug, telegram_id, telegram_name, username, display_name, emoji, "
    "description, aliases_json, status, difficulty, lives, max_lives, "
    "base_pushups, ladder_offset, alt_pushups, max_pushups_reported, "
    "max_squats_reported, joined_date"
)


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA busy_timeout=5000;")
        con.execute("PRAGMA foreign_keys=ON;")
        return con

    # --- СХЕМА ---
    def init_schema(self) -> None:
        with self._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS progress (
                    date TEXT PRIMARY KEY,
                    lyosha_done INTEGER DEFAULT 0,
                    sasha_done INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS users (
                    slug TEXT PRIMARY KEY,
                    telegram_id INTEGER UNIQUE,
                    telegram_name TEXT DEFAULT '',
                    username TEXT DEFAULT '',
                    display_name TEXT NOT NULL,
                    emoji TEXT NOT NULL DEFAULT '🏋️',
                    description TEXT NOT NULL DEFAULT '',
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'uninitialized',
                    difficulty TEXT NOT NULL DEFAULT 'normal',
                    lives INTEGER,
                    max_lives INTEGER,
                    base_pushups INTEGER NOT NULL DEFAULT 10,
                    ladder_offset INTEGER NOT NULL DEFAULT 0,
                    alt_pushups INTEGER,
                    max_pushups_reported INTEGER,
                    max_squats_reported INTEGER,
                    joined_date TEXT NOT NULL DEFAULT '2026-08-31',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS user_progress (
                    date TEXT NOT NULL,
                    user_slug TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (date, user_slug),
                    FOREIGN KEY (user_slug) REFERENCES users(slug)
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
            """)

    # --- USERS ---
    def upsert_user(self, user: User) -> None:
        aliases_json = json.dumps(list(dict.fromkeys(user.aliases)), ensure_ascii=False)
        with self._connect() as con:
            con.execute(f"""
                INSERT INTO users ({_USER_COLS}, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(slug) DO UPDATE SET
                    telegram_id = COALESCE(excluded.telegram_id, users.telegram_id),
                    telegram_name = CASE WHEN excluded.telegram_name != '' THEN excluded.telegram_name ELSE users.telegram_name END,
                    username = CASE WHEN excluded.username != '' THEN excluded.username ELSE users.username END,
                    display_name = excluded.display_name,
                    emoji = excluded.emoji,
                    description = excluded.description,
                    aliases_json = excluded.aliases_json,
                    status = excluded.status,
                    difficulty = excluded.difficulty,
                    lives = excluded.lives,
                    max_lives = excluded.max_lives,
                    base_pushups = excluded.base_pushups,
                    ladder_offset = excluded.ladder_offset,
                    alt_pushups = excluded.alt_pushups,
                    max_pushups_reported = excluded.max_pushups_reported,
                    max_squats_reported = excluded.max_squats_reported,
                    joined_date = excluded.joined_date,
                    updated_at = datetime('now')
            """, (
                user.slug, user.telegram_id, user.telegram_name, user.username,
                user.display_name, user.emoji, user.description, aliases_json,
                user.status, user.difficulty, user.lives, user.max_lives,
                user.base_pushups, user.ladder_offset, user.alt_pushups,
                user.max_pushups_reported, user.max_squats_reported, user.joined_date,
            ))

    def get_user(self, slug: str) -> Optional[User]:
        with self._connect() as con:
            row = con.execute(f"SELECT {_USER_COLS} FROM users WHERE slug = ?", (slug,)).fetchone()
        return User.from_row(row) if row else None

    def list_users(self, status: Optional[str] = None) -> List[User]:
        with self._connect() as con:
            if status:
                rows = con.execute(
                    f"SELECT {_USER_COLS} FROM users WHERE status = ? ORDER BY created_at ASC, slug ASC",
                    (status,),
                ).fetchall()
            else:
                rows = con.execute(f"SELECT {_USER_COLS} FROM users ORDER BY created_at ASC, slug ASC").fetchall()
        return [User.from_row(r) for r in rows]

    def find_user(self, raw: str, telegram_id: Optional[int] = None, username: Optional[str] = None) -> Optional[User]:
        """Шукає користувача за telegram_id, username, slug, ім'ям або псевдонімом (aliases)."""
        users = self.list_users()
        if telegram_id is not None:
            for u in users:
                if u.telegram_id == telegram_id:
                    return u
        clean_username = (username or "").lstrip("@").lower().strip()
        if clean_username:
            for u in users:
                if u.username and u.username.lstrip("@").lower() == clean_username:
                    return u

        val = (raw or "").lower().strip()
        if not val:
            return None

        # 1. Точний збіг за slug, display_name, telegram_name або username
        for u in users:
            if val in (
                u.slug.lower(),
                u.display_name.lower(),
                u.telegram_name.lower(),
                u.username.lstrip("@").lower() if u.username else "",
            ):
                return u

        # 2. Пошук за входженням аліасів (довші аліаси перевіряємо першими, щоб 'олександр' не перехоплювався 'олекс')
        candidates: List[Tuple[int, User]] = []
        for u in users:
            tokens = set(a.lower() for a in u.aliases if a)
            if u.display_name:
                tokens.add(u.display_name.lower())
            if u.telegram_name:
                tokens.add(u.telegram_name.lower())
            for token in tokens:
                if token and token in val:
                    candidates.append((len(token), u))
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            return candidates[0][1]
        return None

    def resolve_or_create_user(
        self,
        user_name: str,
        joined_date: str,
        telegram_id: Optional[int] = None,
        username: Optional[str] = None,
    ) -> User:
        """
        Знаходить користувача в БД або створює нового зі статусом 'uninitialized'.
        Якщо знайдений користувач ще не мав telegram_id / username — прив'язує їх.
        """
        found = self.find_user(user_name, telegram_id=telegram_id, username=username)
        if found is not None:
            changed = False
            if telegram_id is not None and found.telegram_id != telegram_id:
                found.telegram_id = telegram_id
                changed = True
            if username and not found.username:
                found.username = username.lstrip("@")
                changed = True
            if user_name and not found.telegram_name:
                found.telegram_name = user_name
                changed = True
            if changed:
                self.upsert_user(found)
            return found

        base_slug = slugify(username or user_name or (f"user_{telegram_id}" if telegram_id else "user"))
        slug = base_slug
        idx = 2
        while self.get_user(slug) is not None:
            slug = f"{base_slug}_{idx}"
            idx += 1

        aliases = [a for a in {user_name.lower().strip(), (username or "").lstrip("@").lower().strip()} if a]
        new_user = User(
            slug=slug,
            telegram_id=telegram_id,
            telegram_name=user_name or slug,
            username=(username or "").lstrip("@"),
            display_name=user_name or slug,
            emoji="🏋️",
            description="",
            aliases=aliases,
            status="uninitialized",
            difficulty="normal",
            lives=None,
            max_lives=None,
            base_pushups=10,
            ladder_offset=0,
            alt_pushups=None,
            joined_date=joined_date,
        )
        self.upsert_user(new_user)
        logger.info("Created new uninitialized user: slug=%s, name=%s, tg_id=%s", slug, user_name, telegram_id)
        return new_user

    # --- BOT STATE ---
    def get_state(self, key: str, default: str = "") -> str:
        with self._connect() as con:
            row = con.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        if row and row[0] is not None:
            return row[0]
        # Зворотна сумісність із ключами lives_<slug>, offset_<slug>, alt_pushups_<slug>
        for prefix, attr in (("lives_", "lives"), ("offset_", "ladder_offset"), ("alt_pushups_", "alt_pushups")):
            if key.startswith(prefix):
                u = self.get_user(key[len(prefix):])
                val = getattr(u, attr, None) if u else None
                if val is not None:
                    return str(val)
        return default

    def set_state(self, key: str, value) -> None:
        str_val = str(value)
        with self._connect() as con:
            con.execute("""
                INSERT INTO bot_state (key, value, updated_at) VALUES (?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')
            """, (key, str_val))
            # Синхронізація з таблицею users, якщо оновлюється легасі-ключ учасника
            for prefix, col in (("lives_", "lives"), ("offset_", "ladder_offset"), ("alt_pushups_", "alt_pushups")):
                if key.startswith(prefix):
                    slug = key[len(prefix):]
                    if str_val.strip().lstrip("-").isdigit():
                        con.execute(
                            f"UPDATE users SET {col} = ?, updated_at = datetime('now') WHERE slug = ?",
                            (int(str_val), slug),
                        )

    def all_state(self) -> Dict[str, str]:
        with self._connect() as con:
            return dict(con.execute("SELECT key, value FROM bot_state").fetchall())

    # --- PROGRESS ---
    def get_day_progress(self, date_iso: str) -> Dict[str, bool]:
        """Повертає {slug: done} для всіх активних учасників на дату date_iso."""
        active = self.list_users(status="active")
        with self._connect() as con:
            rows = dict(
                con.execute("SELECT user_slug, done FROM user_progress WHERE date = ?", (date_iso,)).fetchall()
            )
            legacy_row = None
            try:
                legacy_row = con.execute(
                    "SELECT lyosha_done, sasha_done FROM progress WHERE date = ?", (date_iso,)
                ).fetchone()
            except sqlite3.OperationalError:
                pass
        legacy = {"lyosha": bool(legacy_row[0]), "sasha": bool(legacy_row[1])} if legacy_row else {}
        result: Dict[str, bool] = {}
        for u in active:
            if u.slug in rows:
                result[u.slug] = bool(rows[u.slug])
            else:
                result[u.slug] = legacy.get(u.slug, False)
        return result

    def get_user_done(self, slug: str, date_iso: str) -> bool:
        with self._connect() as con:
            row = con.execute(
                "SELECT done FROM user_progress WHERE date = ? AND user_slug = ?", (date_iso, slug)
            ).fetchone()
            if row is not None:
                return bool(row[0])
            if slug in ("lyosha", "sasha"):
                try:
                    l_row = con.execute(
                        f"SELECT {slug}_done FROM progress WHERE date = ?", (date_iso,)
                    ).fetchone()
                    return bool(l_row[0]) if l_row else False
                except sqlite3.OperationalError:
                    pass
        return False

    def get_progress(self, date_iso: str) -> Tuple[bool, ...]:
        """Повертає кортеж булевих значень виконання для активних учасників (сумісність зі старими викликами)."""
        day_map = self.get_day_progress(date_iso)
        if not day_map:
            with self._connect() as con:
                try:
                    row = con.execute(
                        "SELECT lyosha_done, sasha_done FROM progress WHERE date = ?", (date_iso,)
                    ).fetchone()
                    return (bool(row[0]), bool(row[1])) if row else (False, False)
                except sqlite3.OperationalError:
                    return (False, False)
        return tuple(day_map.values())

    def set_done(self, participant: str, done: bool, date_iso: str) -> None:
        user = self.get_user(participant) or self.find_user(participant)
        if user is None:
            raise ValueError(f"Unknown participant: {participant}")
        slug = user.slug
        val = 1 if done else 0
        with self._connect() as con:
            con.execute("""
                INSERT INTO user_progress (date, user_slug, done, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(date, user_slug) DO UPDATE SET done = excluded.done, updated_at = datetime('now')
            """, (date_iso, slug, val))

    # --- CHAT HISTORY ---
    def save_message(self, chat_id: int, user_name: str, role: str, text: str, created_at: str) -> int:
        with self._connect() as con:
            cur = con.execute(
                "INSERT INTO chat_history (chat_id, user_name, role, text, created_at) VALUES (?, ?, ?, ?, ?)",
                (chat_id, user_name, role, text, created_at),
            )
            return cur.lastrowid or 0

    def get_last_message_id(self, chat_id: Optional[int] = None) -> int:
        with self._connect() as con:
            if chat_id is not None:
                row = con.execute("SELECT max(id) FROM chat_history WHERE chat_id = ?", (chat_id,)).fetchone()
            else:
                row = con.execute("SELECT max(id) FROM chat_history").fetchone()
            return (row[0] or 0) if row else 0

    def unprocessed_user_messages(
        self, chat_id: int, after_ts: str, day_start_ts: str, after_id: Optional[int] = None
    ) -> List[ChatMessage]:
        with self._connect() as con:
            if after_id is not None:
                rows = con.execute("""
                    SELECT id, user_name, role, text, created_at FROM chat_history
                    WHERE chat_id = ? AND role = 'user'
                      AND (created_at > ? OR (created_at = ? AND id > ?))
                      AND created_at >= ?
                    ORDER BY created_at ASC, id ASC
                """, (chat_id, after_ts, after_ts, int(after_id), day_start_ts)).fetchall()
            else:
                rows = con.execute("""
                    SELECT id, user_name, role, text, created_at FROM chat_history
                    WHERE chat_id = ? AND role = 'user'
                      AND created_at > ?
                      AND created_at >= ?
                    ORDER BY created_at ASC, id ASC
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

    # --- PENDING REPLIES ---
    def add_pending_reply(
        self,
        request_id: str,
        chat_id: int,
        text: str,
        reply_to_message_id: Optional[int] = None,
    ) -> None:
        """Зберігає відповідь для гарантованої доставки після перезапуску (ідемпотентно за request_id)."""
        with self._connect() as con:
            con.execute("""
                INSERT INTO pending_replies (request_id, chat_id, reply_to_message_id, text, status, attempts, created_at)
                VALUES (?, ?, ?, ?, 'pending', 0, datetime('now'))
                ON CONFLICT(request_id) DO NOTHING
            """, (request_id, chat_id, reply_to_message_id, text))

    def list_pending_replies(self, status: str = "pending") -> List[PendingReply]:
        with self._connect() as con:
            rows = con.execute("""
                SELECT request_id, chat_id, reply_to_message_id, text, status, attempts, created_at
                FROM pending_replies
                WHERE status = ?
                ORDER BY created_at ASC, rowid ASC
            """, (status,)).fetchall()
        return [
            PendingReply(
                request_id=r[0],
                chat_id=r[1],
                reply_to_message_id=r[2],
                text=r[3],
                status=r[4],
                attempts=r[5],
                created_at=r[6] or "",
            )
            for r in rows
        ]

    def mark_reply_sent(self, request_id: str) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE pending_replies SET status = 'sent' WHERE request_id = ?",
                (request_id,),
            )

    def increment_reply_attempts(self, request_id: str, max_attempts: int = 3) -> None:
        with self._connect() as con:
            con.execute("""
                UPDATE pending_replies
                SET attempts = attempts + 1,
                    status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE status END
                WHERE request_id = ?
            """, (max_attempts, request_id))


def format_ts(dt: datetime) -> str:
    return dt.strftime(TS_FORMAT)
