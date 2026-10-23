"""
Синхронне сховище бота поверх SQLite: користувачі (users), прогрес (user_progress),
історія чату (chat_history), bot_state, версіоновані промпти агентів (prompts / prompt_history)
та bot_texts (фіксовані відповіді бота / тон-зразки, читані через core.bot_texts.BotTexts).
Кожен виклик відкриває власне з'єднання — безпечно для asyncio.to_thread та для тулів Gemini.
"""
import json
import uuid
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

from core.textguard import ensure_clean

logger = logging.getLogger(__name__)

TS_FORMAT = "%Y-%m-%d %H:%M:%S"

DIFFICULTY_LIVES = {
    "hardcore": 2,
    "normal": 5,
    "welcome": 10,
}

# Таблиці з "гучними" лог-даними, які можна чистити за віком (див. Store.prune_old_logs).
# НЕ включати admin_audit/prompt_history (аудит/історія версій) чи user_progress/progress (історія норм).
LOG_RETENTION_TABLES = ("cv_jobs", "offload_requests")  # chat_history/video_checks = дані челенджу, не чистимо

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
class VideoCheck:
    id: int
    request_id: str
    chat_id: int
    message_id: int
    user_slug: Optional[str] = None
    file_id: Optional[str] = None
    file_unique_id: Optional[str] = None
    duration_s: Optional[float] = None
    exercise: Optional[str] = None
    reps: Optional[int] = None
    confidence: Optional[float] = None
    flags: List[str] = field(default_factory=list)
    valid: bool = False
    status: str = "queued"
    analyzer_version: Optional[str] = None
    location: Optional[str] = None
    created_at: str = ""
    finished_at: Optional[str] = None


@dataclass
class OffloadRequest:
    request_id: str
    type: str
    chat_id: Optional[int] = None
    message_id: Optional[int] = None
    reply_to_message_id: Optional[int] = None
    author_slug: Optional[str] = None
    payload: Optional[str] = None
    status: str = "sent"
    commit_sha: Optional[str] = None
    attempts: int = 0
    expires_at: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class AdminAudit:
    id: int
    request_id: str
    author_slug: Optional[str] = None
    author_telegram_id: Optional[int] = None
    request: Optional[str] = None
    plan: Optional[str] = None
    commit_sha: Optional[str] = None
    status: str = "planned"
    created_at: str = ""
    updated_at: str = ""


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

_VIDEO_CHECK_COLS = (
    "id, request_id, chat_id, message_id, user_slug, file_id, file_unique_id, "
    "duration_s, exercise, reps, confidence, flags, valid, status, analyzer_version, "
    "location, created_at, finished_at"
)


def _row_to_video_check(row: Tuple) -> VideoCheck:
    flags_val = row[11]
    if isinstance(flags_val, str):
        try:
            flags = json.loads(flags_val)
        except Exception:
            flags = []
    elif isinstance(flags_val, list):
        flags = flags_val
    else:
        flags = []
    return VideoCheck(
        id=row[0],
        request_id=row[1],
        chat_id=row[2],
        message_id=row[3],
        user_slug=row[4],
        file_id=row[5],
        file_unique_id=row[6],
        duration_s=row[7],
        exercise=row[8],
        reps=row[9],
        confidence=row[10],
        flags=flags,
        valid=bool(row[12]),
        status=row[13],
        analyzer_version=row[14],
        location=row[15],
        created_at=row[16] or "",
        finished_at=row[17],
    )


_OFFLOAD_REQUEST_COLS = (
    "request_id, type, chat_id, message_id, reply_to_message_id, "
    "author_slug, payload, status, commit_sha, attempts, "
    "expires_at, created_at, updated_at"
)


def _row_to_offload_request(row: Tuple) -> OffloadRequest:
    return OffloadRequest(
        request_id=row[0],
        type=row[1],
        chat_id=row[2],
        message_id=row[3],
        reply_to_message_id=row[4],
        author_slug=row[5],
        payload=row[6],
        status=row[7],
        commit_sha=row[8],
        attempts=row[9],
        expires_at=row[10],
        created_at=row[11] or "",
        updated_at=row[12] or "",
    )


_ADMIN_AUDIT_COLS = (
    "id, request_id, author_slug, author_telegram_id, request, "
    "plan, commit_sha, status, created_at, updated_at"
)


def _row_to_admin_audit(row: Tuple) -> AdminAudit:
    return AdminAudit(
        id=row[0],
        request_id=row[1],
        author_slug=row[2],
        author_telegram_id=row[3],
        request=row[4],
        plan=row[5],
        commit_sha=row[6],
        status=row[7],
        created_at=row[8] or "",
        updated_at=row[9] or "",
    )


def _row_to_bot_text(row: Tuple) -> Dict[str, Any]:
    variants_val = row[2]
    try:
        variants = json.loads(variants_val) if variants_val else None
    except Exception:
        variants = None
    return {
        "key": row[0],
        "text": row[1],
        "variants": variants,
        "updated_by": row[3],
        "updated_at": row[4] or "",
    }


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
                CREATE TABLE IF NOT EXISTS video_checks (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id       TEXT UNIQUE,
                    chat_id          INTEGER NOT NULL,
                    message_id       INTEGER NOT NULL,
                    user_slug        TEXT,
                    file_id          TEXT,
                    file_unique_id   TEXT,
                    duration_s       REAL,
                    exercise         TEXT,
                    reps             INTEGER,
                    confidence       REAL,
                    flags            TEXT,
                    valid            INTEGER DEFAULT 0,
                    status           TEXT NOT NULL DEFAULT 'queued',
                    analyzer_version TEXT,
                    location         TEXT,
                    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    finished_at      TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_video_checks_status ON video_checks(status);
                CREATE INDEX IF NOT EXISTS idx_video_checks_file_unique_id ON video_checks(file_unique_id);
                CREATE TABLE IF NOT EXISTS offload_requests (
                    request_id          TEXT PRIMARY KEY,
                    type                TEXT NOT NULL,
                    chat_id             INTEGER,
                    message_id          INTEGER,
                    reply_to_message_id INTEGER,
                    author_slug         TEXT,
                    payload             TEXT,
                    status              TEXT NOT NULL DEFAULT 'sent',
                    commit_sha          TEXT,
                    attempts            INTEGER DEFAULT 0,
                    expires_at          TIMESTAMP,
                    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_offload_requests_status ON offload_requests(status);
                CREATE TABLE IF NOT EXISTS admin_audit (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id          TEXT NOT NULL,
                    author_slug         TEXT,
                    author_telegram_id  INTEGER,
                    request             TEXT,
                    plan                TEXT,
                    commit_sha          TEXT,
                    status              TEXT NOT NULL DEFAULT 'planned',
                    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_admin_audit_request_id ON admin_audit(request_id);
                CREATE INDEX IF NOT EXISTS idx_admin_audit_status ON admin_audit(status);
                CREATE TABLE IF NOT EXISTS bot_texts (
                    key         TEXT PRIMARY KEY,
                    text        TEXT NOT NULL,
                    variants    TEXT,
                    updated_by  TEXT,
                    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS cv_jobs (
                    request_id        TEXT PRIMARY KEY,
                    status            TEXT NOT NULL DEFAULT 'queued',   -- queued|claimed|done|failed
                    claim_token       TEXT,
                    claimed_at        TIMESTAMP,
                    attempts          INTEGER NOT NULL DEFAULT 0,
                    last_error        TEXT,
                    timings           TEXT,                              -- JSON від worker-а
                    processing_sent_at TIMESTAMP,
                    still_sent_at     TIMESTAMP,
                    replied_at        TIMESTAMP,
                    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_cv_jobs_status ON cv_jobs(status);

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
                CREATE INDEX IF NOT EXISTS idx_admin_tasks_status ON admin_tasks(status);

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

    # --- ADMIN TASKS (запити до розробника, яких не покривають вузькі адмін-тули) ---
    _ADMIN_TASK_COLS = ("id", "chat_id", "requester_telegram_id", "requester_name", "text", "status",
                        "fix_commit", "result", "notified", "created_at", "updated_at")

    def _admin_task_row(self, row) -> Optional[Dict[str, Any]]:
        return dict(zip(self._ADMIN_TASK_COLS, row)) if row else None

    def create_admin_task(self, chat_id: Optional[int], requester_telegram_id: Optional[int],
                          requester_name: str, text: str) -> int:
        ensure_clean(text, "admin_task.text")
        with self._connect() as con:
            cur = con.execute(
                "INSERT INTO admin_tasks (chat_id, requester_telegram_id, requester_name, text) VALUES (?, ?, ?, ?)",
                (chat_id, requester_telegram_id, requester_name, text),
            )
            task_id = int(cur.lastrowid)
        logger.info("admin_task created: id=%s requester=%s/%s text=%r", task_id, requester_telegram_id,
                    requester_name, text[:200])
        return task_id

    def get_admin_task(self, task_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as con:
            row = con.execute(f"SELECT {', '.join(self._ADMIN_TASK_COLS)} FROM admin_tasks WHERE id = ?",
                              (int(task_id),)).fetchone()
        return self._admin_task_row(row)

    # --- ПРОГРЕСІЯ: найкраща спроба дня, XP/рівні, адаптивна наступна норма (міграція 0011) ---
    def record_attempt(self, date_iso: str, slug: str, exercise: str, reps: int) -> int:
        """Зберігає спробу; best_reps = MAX(попередній, reps). Повертає поточний best."""
        with self._connect() as con:
            con.execute(
                "INSERT INTO daily_attempts (date, user_slug, exercise, best_reps) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(date, user_slug) DO UPDATE SET best_reps = MAX(best_reps, excluded.best_reps), "
                "exercise = excluded.exercise, updated_at = CURRENT_TIMESTAMP",
                (date_iso, slug, exercise, max(0, int(reps))),
            )
        best = self.get_best_attempt(date_iso, slug) or 0
        logger.info("Attempt recorded: %s %s %s reps=%s best=%s", date_iso, slug, exercise, reps, best)
        return best

    def set_attempt(self, date_iso: str, slug: str, exercise: str, reps: int) -> int:
        """Ручне встановлення/перезарахування спроби дня (точне значення best_reps = reps)."""
        val = max(0, int(reps))
        with self._connect() as con:
            con.execute(
                "INSERT INTO daily_attempts (date, user_slug, exercise, best_reps) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(date, user_slug) DO UPDATE SET best_reps = excluded.best_reps, "
                "exercise = excluded.exercise, updated_at = CURRENT_TIMESTAMP",
                (date_iso, slug, exercise, val),
            )
        logger.info("Attempt manually set: %s %s %s best_reps=%s", date_iso, slug, exercise, val)
        return val

    def get_best_attempt(self, date_iso: str, slug: str) -> Optional[int]:
        with self._connect() as con:
            row = con.execute("SELECT best_reps FROM daily_attempts WHERE date = ? AND user_slug = ?",
                              (date_iso, slug)).fetchone()
        return int(row[0]) if row else None

    def video_reps_rows(self, slug: str, since_utc: str) -> List[Tuple[str, Optional[str], int]]:
        """(created_at UTC, exercise, reps) з video_checks автора, новіші за since_utc — фолбек для найкращої спроби."""
        with self._connect() as con:
            return [(r[0], r[1], int(r[2] or 0)) for r in con.execute(
                "SELECT created_at, exercise, reps FROM video_checks WHERE user_slug = ? AND reps > 0 AND created_at >= ?",
                (slug, since_utc)).fetchall()]

    def get_xp(self, slug: str) -> Tuple[int, int]:
        with self._connect() as con:
            row = con.execute("SELECT xp, level FROM user_xp WHERE user_slug = ?", (slug,)).fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    def set_xp(self, slug: str, xp: int, level: int) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT INTO user_xp (user_slug, xp, level) VALUES (?, ?, ?) ON CONFLICT(user_slug) DO UPDATE SET "
                "xp = excluded.xp, level = excluded.level, updated_at = CURRENT_TIMESTAMP",
                (slug, int(xp), int(level)),
            )

    def add_xp_log(self, date_iso: str, slug: str, delta: int, reason: str) -> None:
        with self._connect() as con:
            con.execute("INSERT INTO xp_log (date, user_slug, delta, reason) VALUES (?, ?, ?, ?)",
                        (date_iso, slug, int(delta), reason))

    def get_next_norm(self, slug: str, exercise: str) -> Optional[Tuple[int, str]]:
        with self._connect() as con:
            row = con.execute("SELECT target, effective_from FROM next_norms WHERE user_slug = ? AND exercise = ?",
                              (slug, exercise)).fetchone()
        return (int(row[0]), str(row[1])) if row else None

    def set_next_norm(self, slug: str, exercise: str, target: int, effective_from: str,
                      source_date: Optional[str] = None) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT INTO next_norms (user_slug, exercise, target, effective_from, source_date) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_slug, exercise) DO UPDATE SET target = excluded.target, "
                "effective_from = excluded.effective_from, source_date = excluded.source_date",
                (slug, exercise, int(target), effective_from, source_date),
            )

    def clear_next_norm(self, slug: str, exercise: str) -> None:
        with self._connect() as con:
            con.execute("DELETE FROM next_norms WHERE user_slug = ? AND exercise = ?", (slug, exercise))

    def active_admin_task_for(self, requester_telegram_id: Optional[int], requester_name: Optional[str]) -> Optional[int]:
        """id незавершеної (pending/claimed) admin_task цього автора або None."""
        with self._connect() as con:
            row = con.execute(
                "SELECT id FROM admin_tasks WHERE status IN ('pending','claimed') AND "
                "((? IS NOT NULL AND requester_telegram_id = ?) OR (? IS NOT NULL AND requester_name = ?)) "
                "ORDER BY id DESC LIMIT 1",
                (requester_telegram_id, requester_telegram_id, requester_name, requester_name),
            ).fetchone()
        return int(row[0]) if row else None

    def list_stale_admin_tasks(self, hours: float) -> List[Dict[str, Any]]:
        """pending/claimed задачі, старші за `hours` годин (для нагадування «ще в роботі», без скасування)."""
        with self._connect() as con:
            rows = con.execute(
                f"SELECT {', '.join(self._ADMIN_TASK_COLS)} FROM admin_tasks WHERE status IN ('pending','claimed') "
                "AND created_at <= datetime('now', ?) ORDER BY id ASC",
                (f"-{float(hours)} hours",),
            ).fetchall()
        return [self._admin_task_row(r) for r in rows]

    def list_admin_tasks(self, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        q = f"SELECT {', '.join(self._ADMIN_TASK_COLS)} FROM admin_tasks"
        args: list = []
        if status:
            q += " WHERE status = ?"
            args.append(status)
        q += " ORDER BY id ASC LIMIT ?"
        args.append(int(limit))
        with self._connect() as con:
            return [self._admin_task_row(r) for r in con.execute(q, args).fetchall()]

    def claim_admin_task(self) -> Optional[Dict[str, Any]]:
        """Атомарно бере найстарішу pending-задачу і ставить claimed."""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT id FROM admin_tasks WHERE status='pending' ORDER BY id ASC LIMIT 1").fetchone()
            if not row:
                con.commit()
                return None
            con.execute("UPDATE admin_tasks SET status='claimed', updated_at=datetime('now') WHERE id=? AND status='pending'",
                        (row[0],))
            con.commit()
        logger.info("admin_task claimed: id=%s", row[0])
        return self.get_admin_task(row[0])

    def set_admin_task_result(self, task_id: int, status: str, fix_commit: Optional[str] = None,
                              result: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if status not in ("done_pending_deploy", "rejected"):
            raise ValueError(f"invalid admin_task result status: {status!r}")
        ensure_clean(result, "admin_task.result")
        with self._connect() as con:
            cur = con.execute(
                "UPDATE admin_tasks SET status=?, fix_commit=?, result=?, updated_at=datetime('now') "
                "WHERE id=? AND status IN ('pending','claimed')",
                (status, fix_commit, result, int(task_id)),
            )
            changed = cur.rowcount
        if not changed:
            return None
        logger.info("admin_task result: id=%s status=%s fix_commit=%s", task_id, status, fix_commit)
        return self.get_admin_task(task_id)

    def mark_admin_task_announced(self, task_id: int) -> None:
        with self._connect() as con:
            con.execute("UPDATE admin_tasks SET status='announced', notified=1, updated_at=datetime('now') WHERE id=?",
                        (int(task_id),))

    def mark_admin_task_notified(self, task_id: int) -> None:
        with self._connect() as con:
            con.execute("UPDATE admin_tasks SET notified=1, updated_at=datetime('now') WHERE id=?", (int(task_id),))

    # --- CV JOBS (box offload queue, long-poll через gateway) ---
    def enqueue_cv_job(self, request_id: str) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT OR IGNORE INTO cv_jobs (request_id, status, created_at, updated_at) "
                "VALUES (?, 'queued', datetime('now'), datetime('now'))",
                (request_id,),
            )

    def get_cv_job(self, request_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as con:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM cv_jobs WHERE request_id = ?", (request_id,)).fetchone()
        return dict(row) if row else None

    def claim_cv_job(self, lease_seconds: int = 300, max_attempts: int = 3) -> Optional[Dict[str, Any]]:
        """
        Атомарно бере найстаріший queued job (або claimed з простроченим lease -> requeue).
        Job, що вичерпав max_attempts, позначається failed. Повертає dict з claim_token.
        """
        token = uuid.uuid4().hex
        lease = f"-{int(lease_seconds)} seconds"
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE cv_jobs SET status='failed', last_error='lease_expired_max_attempts', updated_at=datetime('now') "
                "WHERE status='claimed' AND claimed_at <= datetime('now', ?) AND attempts >= ?",
                (lease, max_attempts),
            )
            con.execute(
                "UPDATE cv_jobs SET status='queued', claim_token=NULL, updated_at=datetime('now') "
                "WHERE status='claimed' AND claimed_at <= datetime('now', ?)",
                (lease,),
            )
            row = con.execute(
                "SELECT request_id FROM cv_jobs WHERE status='queued' ORDER BY created_at ASC, rowid ASC LIMIT 1"
            ).fetchone()
            if not row:
                con.commit()
                return None
            con.execute(
                "UPDATE cv_jobs SET status='claimed', claim_token=?, claimed_at=datetime('now'), "
                "attempts=attempts+1, updated_at=datetime('now') WHERE request_id=?",
                (token, row[0]),
            )
            con.execute("UPDATE video_checks SET status='running', location='box' WHERE request_id=?", (row[0],))
            con.commit()
        return self.get_cv_job(row[0])

    def complete_cv_job(
        self, request_id: str, claim_token: str, *, ok: bool, error: Optional[str] = None,
        timings: Optional[Dict[str, Any]] = None, max_attempts: int = 3,
    ) -> Optional[str]:
        """Фіксує результат job-а, якщо claim_token збігається. Повертає новий статус або None (stale/unknown)."""
        job = self.get_cv_job(request_id)
        if not job or job["status"] != "claimed" or job["claim_token"] != claim_token:
            return None
        if ok:
            new_status = "done"
        else:
            new_status = "failed" if job["attempts"] >= max_attempts else "queued"
        with self._connect() as con:
            con.execute(
                "UPDATE cv_jobs SET status=?, claim_token=CASE WHEN ?='queued' THEN NULL ELSE claim_token END, "
                "last_error=?, timings=?, updated_at=datetime('now') WHERE request_id=?",
                (new_status, new_status, error, json.dumps(timings or {}, ensure_ascii=False), request_id),
            )
        return new_status

    def mark_cv_job(self, request_id: str, column: str) -> None:
        if column not in ("processing_sent_at", "still_sent_at", "replied_at"):
            raise ValueError(column)
        with self._connect() as con:
            con.execute(f"UPDATE cv_jobs SET {column}=datetime('now') WHERE request_id=?", (request_id,))

    def list_cv_jobs_to_reply(self) -> List[Dict[str, Any]]:
        with self._connect() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM cv_jobs WHERE status IN ('done','failed') AND replied_at IS NULL ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def list_cv_jobs_overdue(self, minutes: int) -> List[Dict[str, Any]]:
        with self._connect() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM cv_jobs WHERE status IN ('queued','claimed') AND still_sent_at IS NULL "
                "AND created_at <= datetime('now', ?)",
                (f"-{int(minutes)} minutes",),
            ).fetchall()
        return [dict(r) for r in rows]

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

    # --- BOT TEXTS (фіксовані відповіді бота / тон-зразки, правка без деплою) ---
    def get_bot_text(self, key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as con:
            row = con.execute(
                "SELECT key, text, variants, updated_by, updated_at FROM bot_texts WHERE key = ?",
                (key,),
            ).fetchone()
        return _row_to_bot_text(row) if row else None

    def set_bot_text(
        self,
        key: str,
        text: str,
        variants: Optional[List[str]] = None,
        updated_by: Optional[str] = None,
    ) -> None:
        ensure_clean(text, f"bot_texts.{key}")
        for v in variants or []:
            ensure_clean(v, f"bot_texts.{key} variant")
        variants_json = json.dumps(variants, ensure_ascii=False) if variants is not None else None
        with self._connect() as con:
            con.execute("""
                INSERT INTO bot_texts (key, text, variants, updated_by, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    text = excluded.text, variants = excluded.variants,
                    updated_by = excluded.updated_by, updated_at = datetime('now')
            """, (key, text, variants_json, updated_by))

    def list_bot_texts(self) -> List[Dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT key, text, variants, updated_by, updated_at FROM bot_texts ORDER BY key"
            ).fetchall()
        return [_row_to_bot_text(r) for r in rows]

    def seed_bot_texts(self, defaults: Dict[str, str]) -> List[str]:
        """Ідемпотентно додає відсутні bot_texts (INSERT OR IGNORE) — ніколи не чіпає адмінські правки."""
        seeded = []
        with self._connect() as con:
            for key, text in defaults.items():
                cur = con.execute(
                    "INSERT OR IGNORE INTO bot_texts (key, text, updated_by, updated_at) "
                    "VALUES (?, ?, 'seed', datetime('now'))",
                    (key, text),
                )
                if cur.rowcount:
                    seeded.append(key)
        return seeded

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
        ensure_clean(content, f"prompts.{key}")
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

    # --- VIDEO CHECKS ---
    def create_video_check(
        self,
        request_id: str,
        chat_id: int,
        message_id: int,
        user_slug: Optional[str] = None,
        file_id: Optional[str] = None,
        file_unique_id: Optional[str] = None,
        duration_s: Optional[float] = None,
        exercise: Optional[str] = None,
        reps: Optional[int] = None,
        confidence: Optional[float] = None,
        flags: Optional[List[str]] = None,
        valid: bool = False,
        status: str = "queued",
        analyzer_version: Optional[str] = None,
        location: Optional[str] = "vm",
    ) -> VideoCheck:
        flags_json = json.dumps(flags or [], ensure_ascii=False)
        with self._connect() as con:
            cur = con.execute("""
                INSERT INTO video_checks (
                    request_id, chat_id, message_id, user_slug, file_id, file_unique_id,
                    duration_s, exercise, reps, confidence, flags, valid, status,
                    analyzer_version, location, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """, (
                request_id, chat_id, message_id, user_slug, file_id, file_unique_id,
                duration_s, exercise, reps, confidence, flags_json, 1 if valid else 0, status,
                analyzer_version, location,
            ))
            row_id = cur.lastrowid
        res = self.get_video_check_by_id(row_id)
        if res is None:
            raise RuntimeError(f"Failed to create video_check for request_id={request_id}")
        return res

    def get_video_check(self, request_id: str) -> Optional[VideoCheck]:
        with self._connect() as con:
            row = con.execute(
                f"SELECT {_VIDEO_CHECK_COLS} FROM video_checks WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return _row_to_video_check(row) if row else None

    def get_video_check_by_id(self, check_id: int) -> Optional[VideoCheck]:
        with self._connect() as con:
            row = con.execute(
                f"SELECT {_VIDEO_CHECK_COLS} FROM video_checks WHERE id = ?",
                (check_id,),
            ).fetchone()
        return _row_to_video_check(row) if row else None

    def get_video_check_by_file_unique_id(self, file_unique_id: str) -> Optional[VideoCheck]:
        with self._connect() as con:
            row = con.execute(
                f"SELECT {_VIDEO_CHECK_COLS} FROM video_checks WHERE file_unique_id = ? ORDER BY id DESC LIMIT 1",
                (file_unique_id,),
            ).fetchone()
        return _row_to_video_check(row) if row else None

    def claim_next_video_check(self) -> Optional[VideoCheck]:
        """Атомарно переводить один найстаріший запис зі status='queued' у status='running' (concurrency=1)."""
        with self._connect() as con:
            cur = con.cursor()
            cur.execute("""
                SELECT id FROM video_checks
                WHERE status = 'queued'
                ORDER BY id ASC LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return None
            check_id = row[0]
            cur.execute("""
                UPDATE video_checks
                SET status = 'running'
                WHERE id = ? AND status = 'queued'
            """, (check_id,))
            if cur.rowcount == 0:
                return None
            row_updated = cur.execute(
                f"SELECT {_VIDEO_CHECK_COLS} FROM video_checks WHERE id = ?",
                (check_id,),
            ).fetchone()
            return _row_to_video_check(row_updated) if row_updated else None

    def update_video_check(
        self,
        request_id: str,
        *,
        status: Optional[str] = None,
        exercise: Optional[str] = None,
        reps: Optional[int] = None,
        confidence: Optional[float] = None,
        flags: Optional[List[str]] = None,
        valid: Optional[bool] = None,
        analyzer_version: Optional[str] = None,
        finished: bool = False,
    ) -> None:
        updates = []
        params = []
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if exercise is not None:
            updates.append("exercise = ?")
            params.append(exercise)
        if reps is not None:
            updates.append("reps = ?")
            params.append(reps)
        if confidence is not None:
            updates.append("confidence = ?")
            params.append(confidence)
        if flags is not None:
            updates.append("flags = ?")
            params.append(json.dumps(flags, ensure_ascii=False))
        if valid is not None:
            updates.append("valid = ?")
            params.append(1 if valid else 0)
        if analyzer_version is not None:
            updates.append("analyzer_version = ?")
            params.append(analyzer_version)
        if finished:
            updates.append("finished_at = datetime('now')")
        if not updates:
            return
        params.append(request_id)
        with self._connect() as con:
            con.execute(f"UPDATE video_checks SET {', '.join(updates)} WHERE request_id = ?", tuple(params))

    def list_video_checks(self, status: Optional[str] = None) -> List[VideoCheck]:
        query = f"SELECT {_VIDEO_CHECK_COLS} FROM video_checks"
        params: tuple = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY id ASC"
        with self._connect() as con:
            rows = con.execute(query, params).fetchall()
        return [_row_to_video_check(r) for r in rows]

    def has_video_check_today(self, user_slug: str, date_iso: Union[str, Any]) -> bool:
        """Перевіряє, чи є в таблиці video_checks запис для user_slug за date_iso (будь-який статус)."""
        if hasattr(date_iso, "strftime"):
            date_str = date_iso.strftime("%Y-%m-%d")
        elif hasattr(date_iso, "isoformat"):
            date_str = date_iso.isoformat()[:10]
        else:
            date_str = str(date_iso)[:10]
        with self._connect() as con:
            row = con.execute(
                "SELECT 1 FROM video_checks WHERE user_slug = ? AND (substr(created_at, 1, 10) = ? OR created_at LIKE ?) LIMIT 1",
                (user_slug, date_str, f"{date_str}%"),
            ).fetchone()
        return bool(row)

    def override_today_video_check(
        self,
        user_slug: str,
        date_iso: Union[str, Any],
        reps: int,
        valid: bool,
        exercise: Optional[str] = None,
    ) -> Optional[VideoCheck]:
        """Оновлює останній запис video_checks учасника за вказану дату (ручне перезарахування тренером/адміном)."""
        if hasattr(date_iso, "strftime"):
            date_str = date_iso.strftime("%Y-%m-%d")
        elif hasattr(date_iso, "isoformat"):
            date_str = date_iso.isoformat()[:10]
        else:
            date_str = str(date_iso)[:10]
        with self._connect() as con:
            row = con.execute(
                "SELECT id, request_id FROM video_checks WHERE user_slug = ? AND (substr(created_at, 1, 10) = ? OR created_at LIKE ?) ORDER BY id DESC LIMIT 1",
                (user_slug, date_str, f"{date_str}%"),
            ).fetchone()
            if not row:
                return None
            check_id = int(row[0])
            if exercise is not None:
                con.execute(
                    "UPDATE video_checks SET reps = ?, valid = ?, exercise = ?, status = 'done', finished_at = COALESCE(finished_at, datetime('now')) WHERE id = ?",
                    (max(0, int(reps)), 1 if valid else 0, exercise, check_id),
                )
            else:
                con.execute(
                    "UPDATE video_checks SET reps = ?, valid = ?, status = 'done', finished_at = COALESCE(finished_at, datetime('now')) WHERE id = ?",
                    (max(0, int(reps)), 1 if valid else 0, check_id),
                )
        return self.get_video_check_by_id(check_id)

    # --- OFFLOAD REQUESTS ---
    def create_offload_request(
        self,
        request_id: str,
        type: str,
        chat_id: Optional[int] = None,
        message_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
        author_slug: Optional[str] = None,
        payload: Optional[Union[str, dict]] = None,
        status: str = "sent",
        commit_sha: Optional[str] = None,
        expires_at: Optional[Union[str, datetime]] = None,
    ) -> OffloadRequest:
        payload_str = json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else payload
        expires_at_str = (
            expires_at.strftime(TS_FORMAT) if isinstance(expires_at, datetime) else expires_at
        )
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO offload_requests (
                    request_id, type, chat_id, message_id, reply_to_message_id,
                    author_slug, payload, status, commit_sha, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(request_id) DO NOTHING
                """,
                (
                    request_id,
                    type,
                    chat_id,
                    message_id,
                    reply_to_message_id,
                    author_slug,
                    payload_str,
                    status,
                    commit_sha,
                    expires_at_str,
                ),
            )
        req = self.get_offload_request(request_id)
        if req is None:
            raise RuntimeError(f"Failed to create or retrieve offload_request for request_id={request_id}")
        return req

    def get_offload_request(self, request_id: str) -> Optional[OffloadRequest]:
        with self._connect() as con:
            row = con.execute(
                f"SELECT {_OFFLOAD_REQUEST_COLS} FROM offload_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return _row_to_offload_request(row) if row else None

    def update_offload_request(
        self,
        request_id: str,
        *,
        status: Optional[str] = None,
        commit_sha: Optional[str] = None,
        attempts: Optional[int] = None,
        expires_at: Optional[Union[str, datetime]] = None,
        payload: Optional[Union[str, dict]] = None,
    ) -> None:
        updates: List[str] = []
        params: List[Any] = []
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if commit_sha is not None:
            updates.append("commit_sha = ?")
            params.append(commit_sha)
        if attempts is not None:
            updates.append("attempts = ?")
            params.append(attempts)
        if expires_at is not None:
            expires_at_str = (
                expires_at.strftime(TS_FORMAT) if isinstance(expires_at, datetime) else expires_at
            )
            updates.append("expires_at = ?")
            params.append(expires_at_str)
        if payload is not None:
            updates.append("payload = ?")
            params.append(json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else payload)

        updates.append("updated_at = datetime('now')")
        if not updates:
            return
        params.append(request_id)

        with self._connect() as con:
            con.execute(
                f"UPDATE offload_requests SET {', '.join(updates)} WHERE request_id = ?",
                tuple(params),
            )

    def list_offload_requests(self, status: Optional[str] = None) -> List[OffloadRequest]:
        query = f"SELECT {_OFFLOAD_REQUEST_COLS} FROM offload_requests"
        params: tuple = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY created_at ASC"
        with self._connect() as con:
            rows = con.execute(query, params).fetchall()
        return [_row_to_offload_request(r) for r in rows]

    def get_expired_offload_requests(
        self, now_ts: Optional[Union[str, datetime]] = None
    ) -> List[OffloadRequest]:
        if now_ts is None:
            now_str = datetime.now().strftime(TS_FORMAT)
        elif isinstance(now_ts, datetime):
            now_str = now_ts.strftime(TS_FORMAT)
        else:
            now_str = str(now_ts)

        query = (
            f"SELECT {_OFFLOAD_REQUEST_COLS} FROM offload_requests "
            "WHERE status IN ('sent', 'acked') AND expires_at IS NOT NULL AND expires_at < ? "
            "ORDER BY created_at ASC"
        )
        with self._connect() as con:
            rows = con.execute(query, (now_str,)).fetchall()
        return [_row_to_offload_request(r) for r in rows]

    # --- ADMIN AUDIT (T-52) ---
    def create_admin_audit(
        self,
        request_id: str,
        *,
        author_slug: Optional[str] = None,
        author_telegram_id: Optional[int] = None,
        request: Optional[str] = None,
        plan: Optional[str] = None,
        commit_sha: Optional[str] = None,
        status: str = "planned",
    ) -> AdminAudit:
        with self._connect() as con:
            cur = con.cursor()
            cur.execute(
                """
                INSERT INTO admin_audit (
                    request_id, author_slug, author_telegram_id, request,
                    plan, commit_sha, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                """,
                (
                    request_id,
                    author_slug,
                    author_telegram_id,
                    request,
                    plan or "",
                    commit_sha,
                    status,
                ),
            )
        audit = self.get_admin_audit(request_id)
        if audit is None:
            raise RuntimeError(f"Failed to create admin_audit for request_id={request_id}")
        return audit

    def get_admin_audit(self, request_id: str) -> Optional[AdminAudit]:
        with self._connect() as con:
            row = con.execute(
                f"SELECT {_ADMIN_AUDIT_COLS} FROM admin_audit WHERE request_id = ? ORDER BY id DESC LIMIT 1",
                (request_id,),
            ).fetchone()
        return _row_to_admin_audit(row) if row else None

    def update_admin_audit(
        self,
        request_id: str,
        *,
        status: Optional[str] = None,
        plan: Optional[str] = None,
        commit_sha: Optional[str] = None,
    ) -> None:
        updates: List[str] = []
        params: List[Any] = []
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if plan is not None:
            updates.append("plan = ?")
            params.append(plan)
        if commit_sha is not None:
            updates.append("commit_sha = ?")
            params.append(commit_sha)

        updates.append("updated_at = datetime('now')")
        if not updates:
            return
        params.append(request_id)
        with self._connect() as con:
            con.execute(
                f"UPDATE admin_audit SET {', '.join(updates)} WHERE request_id = ?",
                tuple(params),
            )

    def list_admin_audits(self, status: Optional[str] = None, limit: int = 50) -> List[AdminAudit]:
        query = f"SELECT {_ADMIN_AUDIT_COLS} FROM admin_audit"
        params: tuple = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY id DESC LIMIT ?"
        with self._connect() as con:
            rows = con.execute(query, (*params, limit)).fetchall()
        return [_row_to_admin_audit(r) for r in rows]

    def count_recent_admin_audits(
        self,
        author_slug: Optional[str] = None,
        author_telegram_id: Optional[int] = None,
        since_dt: Optional[datetime] = None,
    ) -> int:
        if since_dt is None:
            cutoff = (datetime.now() - timedelta(hours=1)).strftime(TS_FORMAT)
        else:
            cutoff = since_dt.strftime(TS_FORMAT)

        query = "SELECT COUNT(*) FROM admin_audit WHERE created_at >= ?"
        params: List[Any] = [cutoff]
        if author_slug is not None:
            query += " AND author_slug = ?"
            params.append(author_slug)
        elif author_telegram_id is not None:
            query += " AND author_telegram_id = ?"
            params.append(author_telegram_id)

        with self._connect() as con:
            row = con.execute(query, tuple(params)).fetchone()
        return row[0] if row else 0

    def get_active_admin_audit(
        self,
        author_slug: Optional[str] = None,
        author_telegram_id: Optional[int] = None,
    ) -> Optional[AdminAudit]:
        active_statuses = ("planned", "waiting_approval", "approved", "running", "sent", "acked")
        placeholders = ",".join("?" for _ in active_statuses)
        query = f"SELECT {_ADMIN_AUDIT_COLS} FROM admin_audit WHERE status IN ({placeholders})"
        params: List[Any] = list(active_statuses)
        if author_slug is not None:
            query += " AND author_slug = ?"
            params.append(author_slug)
        elif author_telegram_id is not None:
            query += " AND author_telegram_id = ?"
            params.append(author_telegram_id)
        query += " ORDER BY id DESC LIMIT 1"
        with self._connect() as con:
            row = con.execute(query, tuple(params)).fetchone()
        return _row_to_admin_audit(row) if row else None

    # --- LOG RETENTION ---
    def prune_old_logs(self, before_ts: str) -> Dict[str, int]:
        """
        Видаляє рядки старші за `before_ts` ('YYYY-MM-DD HH:MM:SS') із таблиць LOG_RETENTION_TABLES
        (cv_jobs, offload_requests — технічні службові записи обробки відео/офлоаду). НЕ чіпає
        chat_history/video_checks (дані челенджу — історія чату та звітів), admin_audit/prompt_history
        (аудит і історія версій промптів) ні user_progress/progress (історія норм).
        Повертає {таблиця: кількість_видалених_рядків}.
        """
        deleted: Dict[str, int] = {}
        with self._connect() as con:
            for table in LOG_RETENTION_TABLES:
                cur = con.execute(f"DELETE FROM {table} WHERE created_at < ?", (before_ts,))
                deleted[table] = cur.rowcount
        return deleted

    # --- RECONCILE PROMPTS (T-53) ---
    def get_unreconciled_prompts(self) -> List[Dict[str, Any]]:
        """Знаходить усі активні промпти, оновлені аварійним bot_fallback."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT key, content, version, updated_by, updated_at FROM prompts WHERE updated_by = 'bot_fallback'"
            ).fetchall()
        return [
            {"key": r[0], "content": r[1], "version": r[2], "updated_by": r[3], "updated_at": r[4]}
            for r in rows
        ]


def format_ts(dt: datetime) -> str:
    return dt.strftime(TS_FORMAT)


def apply_prompt_migration(conn: sqlite3.Connection, key: str, content: str, updated_by: str = "git_migration") -> int:
    """Застосовує міграцію промпту в БД, оновлюючи prompts і архівуючи в prompt_history."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prompts (
            key         TEXT PRIMARY KEY,
            content     TEXT NOT NULL,
            version     INTEGER DEFAULT 1,
            updated_by  TEXT DEFAULT 'seed',
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prompt_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            key         TEXT NOT NULL,
            content     TEXT NOT NULL,
            version     INTEGER NOT NULL,
            updated_by  TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    row = conn.execute("SELECT version FROM prompts WHERE key = ?", (key,)).fetchone()
    version = (row[0] + 1) if row else 1
    conn.execute(
        """
        INSERT INTO prompts (key, content, version, updated_by, updated_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET
            content = excluded.content,
            version = excluded.version,
            updated_by = excluded.updated_by,
            updated_at = datetime('now')
        """,
        (key, content, version, updated_by),
    )
    conn.execute(
        "INSERT INTO prompt_history (key, content, version, updated_by) VALUES (?, ?, ?, ?)",
        (key, content, version, updated_by),
    )
    return version
