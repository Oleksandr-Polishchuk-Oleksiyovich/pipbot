"""
core/guardrails.py: Guardrails, forbidden zones detection, admin allowlist, and rate limiting (T-52).
Protects SportNerdBot from unapproved destructive changes or unauthorized admin_dev actions.
"""
import fnmatch
import logging
import os
import re
from datetime import datetime, timedelta
from typing import List, Optional, Sequence, Tuple, Union

from core.config import ADMIN_DEV_RATE_LIMIT_HOUR, ADMIN_TELEGRAM_IDS
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


def check_active_request_limit(
    store: Store,
    author_slug: Optional[str] = None,
    author_telegram_id: Optional[int] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Перевіряє, чи є вже активний admin_dev запит у системі (один активний запит одночасно).
    Повертає (allowed, active_request_id_or_none).
    """
    active = store.get_active_admin_audit(
        author_slug=author_slug,
        author_telegram_id=author_telegram_id,
    )
    if active is not None:
        return (False, active.request_id)
    return (True, None)
