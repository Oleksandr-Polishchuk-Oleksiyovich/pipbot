"""
core/guardrails.py: Guardrails, forbidden zones detection, admin allowlist, and rate limiting (T-52).
Protects SportNerdBot from unapproved destructive changes or unauthorized admin_dev actions.
"""
import fnmatch
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Sequence, Tuple, Union

from core.config import (
    ADMIN_ACTIVE_REQUEST_TTL_HOURS,
    ADMIN_DEV_RATE_LIMIT_HOUR,
    ADMIN_TELEGRAM_IDS,
)
from database.store import Store

logger = logging.getLogger(__name__)

FORBIDDEN_PATTERNS = [
    ".env*",
    "*.env",
    "*secret*",
    "*credential*",
    "*token*",
    "deploy/*",
    "deploy.sh",
    "deploy.ps1",
    "systemd/*",
    "*.service",
    "*.timer",
    ".github/*",
    "workflows/*",
    ".github/workflows/*",
    "migrations/destructive/*",
    "*destructive*",
    # Захищене ядро довіри (ПРАВИЛА ДОВІРИ + приклади): зміна лише з явним /approve (admin_task/PR), не адмін-промптом.
    "prompts_core/*",
    "core/trust_core.py",
]

DESTRUCTIVE_SQL_KEYWORDS = [
    r"\bDROP\s+TABLE\b",
    r"\bDROP\s+COLUMN\b",
    r"\bTRUNCATE\b",
    r"\bDELETE\s+FROM\s+(user_)?progress\b",
    r"\bDELETE\s+FROM\s+users\b",
    r"\bUPDATE\s+(user_)?progress\s+SET\b",
]


def is_forbidden_path(path: str) -> bool:
    """Перевіряє, чи шлях до файлу потрапляє у заборонену зону."""
    norm = path.strip().replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    norm = norm.lstrip("/")

    # Перевірка назви файлу та шляху
    basename = os.path.basename(norm)
    for pattern in FORBIDDEN_PATTERNS:
        if fnmatch.fnmatch(norm, pattern) or fnmatch.fnmatch(basename, pattern):
            return True
        # Перевірка підкаталогів
        parts = norm.split("/")
        for p in parts:
            if fnmatch.fnmatch(p, pattern):
                return True
    return False


def is_destructive_sql(sql: str) -> bool:
    """Перевіряє SQL-код на наявність деструктивних дій (DROP, DELETE історичних даних)."""
    if not sql:
        return False
    for pat in DESTRUCTIVE_SQL_KEYWORDS:
        if re.search(pat, sql, re.IGNORECASE):
            return True
    return False


def check_changes_forbidden(
    paths: Sequence[str], sql_content: Optional[str] = None
) -> Tuple[bool, List[str]]:
    """
    Перевіряє список змінених файлів та опціональний SQL вміст.
    Повертає (is_forbidden, reasons).
    """
    reasons: List[str] = []
    for p in paths:
        if is_forbidden_path(p):
            reasons.append(f"Заборонений шлях: '{p}'")

    if sql_content and is_destructive_sql(sql_content):
        reasons.append("Виявлено деструктивні SQL-інструкції (DROP/DELETE/TRUNCATE)")

    return (len(reasons) > 0, reasons)


def is_admin_allowed(
    telegram_id: Optional[int],
    allowlist: Optional[List[int]] = None,
) -> bool:
    """
    Перевіряє telegram_id адміна за allowlist.
    Якщо allowlist порожній, доступ дозволено за кодовою фразою.
    """
    allowed_ids = allowlist if allowlist is not None else ADMIN_TELEGRAM_IDS
    if not allowed_ids:
        return True
    if telegram_id is None:
        return False
    return int(telegram_id) in allowed_ids


def check_admin_rate_limit(
    store: Store,
    author_slug: Optional[str] = None,
    author_telegram_id: Optional[int] = None,
    limit_per_hour: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """
    Перевіряє rate-limit на кількість admin_dev запитів на годину (≤ N/год).
    Повертає (allowed, error_message).
    """
    limit = limit_per_hour if limit_per_hour is not None else ADMIN_DEV_RATE_LIMIT_HOUR
    now_dt = now or datetime.now()
    since_dt = now_dt - timedelta(hours=1)

    count = store.count_recent_admin_audits(
        author_slug=author_slug,
        author_telegram_id=author_telegram_id,
        since_dt=since_dt,
    )
    if count >= limit:
        return (
            False,
            f"Ліміт admin_dev запитів перевищено ({count}/{limit} за останню годину). Зачекайте перед повторною спробою.",
        )
    return (True, "")


def _parse_utc_dt(val: Any) -> Optional[datetime]:
    if not val:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is not None:
            return val.astimezone(timezone.utc).replace(tzinfo=None)
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                return dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except Exception:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
            try:
                return datetime.strptime(s[:19] if fmt == "%Y-%m-%d %H:%M:%S" else s, fmt)
            except Exception:
                continue
    return None


def check_active_request_limit(
    store: Store,
    author_slug: Optional[str] = None,
    author_telegram_id: Optional[int] = None,
    now: Optional[datetime] = None,
    ttl_hours: Optional[float] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Перевіряє, чи є вже активний admin_dev запит у системі (один активний запит одночасно).
    Ігнорує та позначає як 'expired' застарілі запити, чий created_at старіший за TTL.
    Повертає (allowed, active_request_id_or_none).
    """
    ttl = ttl_hours if ttl_hours is not None else ADMIN_ACTIVE_REQUEST_TTL_HOURS
    if now is not None:
        if now.tzinfo is not None:
            now_utc = now.astimezone(timezone.utc).replace(tzinfo=None)
        else:
            now_utc = now
    else:
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    while True:
        active = store.get_active_admin_audit(
            author_slug=author_slug,
            author_telegram_id=author_telegram_id,
        )
        if active is None:
            return (True, None)

        created_dt = _parse_utc_dt(active.created_at)
        is_stale = False
        if created_dt is None:
            is_stale = True
        elif (now_utc - created_dt) >= timedelta(hours=ttl):
            is_stale = True

        if is_stale:
            logger.info(
                "Expiring stale active admin_audit request_id=%s author_slug=%s created_at=%s (ttl=%sh)",
                active.request_id,
                active.author_slug,
                active.created_at,
                ttl,
            )
            store.update_admin_audit(active.request_id, status="expired")
            continue

        return (False, active.request_id)
