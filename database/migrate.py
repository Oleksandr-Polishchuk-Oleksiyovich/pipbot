"""
Ідемпотентний versioned migration runner для SportNerdBot.
Керує версіонуванням схеми SQLite через таблицю schema_version,
підтримує .sql та .py (upgrade(conn)) міграції,
автоматично робить online .backup перед накатом,
детектує існуючі legacy-БД (stamp v1).
"""
import argparse
import importlib.util
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MIGRATION_FILE_RE = re.compile(r"^(\d{4})_(.+)\.(sql|py)$")


def get_default_migrations_dir() -> str:
    # Корінь проєкту: ../migrations
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, "migrations")


def get_default_backup_dir(db_path: str) -> str:
    try:
        from core.config import BACKUP_DIR
        if BACKUP_DIR:
            return BACKUP_DIR
    except Exception:
        pass
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")


def perform_backup(conn: sqlite3.Connection, db_path: str, backup_dir: Optional[str] = None) -> Optional[str]:
    """Створює online backup БД у backup_dir/<ts>.db через sqlite3 Backup API."""
    if not os.path.exists(db_path) or os.path.getsize(db_path) == 0:
        return None

    target_dir = backup_dir or get_default_backup_dir(db_path)
    os.makedirs(target_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(target_dir, f"{ts}.db")

    # Якщо файл уже існує (кілька викликів за секунду), додаємо мікросекунди
    if os.path.exists(backup_file):
        ts_micro = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_file = os.path.join(target_dir, f"{ts_micro}.db")

    dest_conn = sqlite3.connect(backup_file)
    try:
        conn.backup(dest_conn)
        logger.info("Database backup created at: %s", backup_file)
        return backup_file
    finally:
        dest_conn.close()


def ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()


def check_and_stamp_legacy(conn: sqlite3.Connection) -> bool:
    """
    Якщо users існує і schema_version порожня → stamp v1 (0001_init).
    Повертає True, якщо застамповано.
    """
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM schema_version")
    count = cur.fetchone()[0]
    if count == 0:
        cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'")
        if cur.fetchone() is not None:
            cur.execute(
                "INSERT INTO schema_version (version, name, applied_at) VALUES (1, '0001_init', datetime('now'))"
            )
            conn.commit()
            logger.info("Legacy database detected: stamped version 1 (0001_init)")
            return True
    return False


def discover_migrations(migrations_dir: str) -> List[Tuple[int, str, str, str]]:
    """
    Повертає список [(version, name, full_path, extension)] відсортований за версією.
    """
    if not os.path.exists(migrations_dir):
        return []

    migrations = []
    for fname in sorted(os.listdir(migrations_dir)):
        m = MIGRATION_FILE_RE.match(fname)
        if m:
            version = int(m.group(1))
            name = f"{m.group(1)}_{m.group(2)}"
            ext = m.group(3)
            full_path = os.path.join(migrations_dir, fname)
            migrations.append((version, name, full_path, ext))

    migrations.sort(key=lambda x: x[0])
    return migrations


def get_applied_migrations(db_path: str) -> List[Tuple[int, str, str]]:
    """Повертає [(version, name, applied_at)] з schema_version."""
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    try:
        ensure_schema_version_table(conn)
        check_and_stamp_legacy(conn)
        rows = conn.execute(
            "SELECT version, name, applied_at FROM schema_version ORDER BY version ASC"
        ).fetchall()
        return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]
    finally:
        conn.close()


def get_current_version(db_path: str) -> int:
    applied = get_applied_migrations(db_path)
    return applied[-1][0] if applied else 0


def get_migration_status(db_path: str, migrations_dir: Optional[str] = None) -> Dict:
    m_dir = migrations_dir or get_default_migrations_dir()
    all_migrations = discover_migrations(m_dir)
    applied = get_applied_migrations(db_path)
    applied_map = {v: (n, a) for v, n, a in applied}

    applied_list = []
    pending_list = []
    for ver, name, path, ext in all_migrations:
        if ver in applied_map:
            applied_list.append((ver, name, applied_map[ver][1]))
        else:
            pending_list.append((ver, name, path, ext))

    current_ver = applied[-1][0] if applied else 0
    return {
        "current_version": current_ver,
        "applied": applied_list,
        "pending": pending_list,
    }


def _execute_sql_migration(conn: sqlite3.Connection, file_path: str) -> None:
    with open(file_path, "r", encoding="utf-8") as f:
        sql = f.read()
    conn.executescript(sql)


def _execute_py_migration(conn: sqlite3.Connection, file_path: str) -> None:
    mod_name = os.path.splitext(os.path.basename(file_path))[0]
    spec = importlib.util.spec_from_file_location(f"migrations.{mod_name}", file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load migration module from {file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "upgrade"):
        raise AttributeError(f"Migration {file_path} must define an upgrade(conn: sqlite3.Connection) function")
    module.upgrade(conn)


def run_migrations(
    db_path: str,
    migrations_dir: Optional[str] = None,
    backup_dir: Optional[str] = None,
) -> List[Tuple[int, str]]:
    """
    Застосовує всі не застосовані міграції по порядку в одній транзакції на файл.
    Повертає список застосованих міграцій [(version, name)].
    """
    m_dir = migrations_dir or get_default_migrations_dir()
    all_migrations = discover_migrations(m_dir)

    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    applied_this_run: List[Tuple[int, str]] = []

    try:
        # WAL та pragmas
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")

        ensure_schema_version_table(conn)
        check_and_stamp_legacy(conn)

        cur = conn.cursor()
        cur.execute("SELECT version FROM schema_version")
        applied_versions = {row[0] for row in cur.fetchall()}

        pending = [m for m in all_migrations if m[0] not in applied_versions]

        if not pending:
            logger.info("Database %s is up-to-date (version %s)", db_path, get_current_version(db_path))
            return []

        # Backup перед застосуванням міграцій, якщо БД існує та містить дані
        perform_backup(conn, db_path, backup_dir=backup_dir)

        for version, name, path, ext in pending:
            logger.info("Applying migration %s (v%d)...", name, version)
            try:
                conn.execute("BEGIN IMMEDIATE")
                if ext == "sql":
                    _execute_sql_migration(conn, path)
                elif ext == "py":
                    _execute_py_migration(conn, path)
                else:
                    raise ValueError(f"Unsupported migration extension: {ext}")

                conn.execute(
                    "INSERT INTO schema_version (version, name, applied_at) VALUES (?, ?, datetime('now'))",
                    (version, name),
                )
                conn.commit()
                applied_this_run.append((version, name))
                logger.info("Successfully applied migration %s (v%d)", name, version)
            except Exception as e:
                conn.rollback()
                logger.error("Failed to apply migration %s: %s", name, e)
                raise
    finally:
        conn.close()

    return applied_this_run


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SportNerdBot SQLite migration runner")
    parser.add_argument("--db", default=None, help="Path to SQLite database (default: DB_PATH from core.config)")
    parser.add_argument("--backup-dir", default=None, help="Directory for database backups")
    parser.add_argument("--migrations-dir", default=None, help="Directory containing migration files")
    parser.add_argument("--status", action="store_true", help="Show current migration status without applying")
    args = parser.parse_args(argv)

    db_path = args.db
    if not db_path:
        try:
            from core.config import DB_PATH
            db_path = DB_PATH
        except Exception:
            db_path = "challenge.db"

    if args.status:
        status = get_migration_status(db_path, args.migrations_dir)
        print(f"Database: {db_path}")
        print(f"Current schema version: {status['current_version']}")
        print("Applied migrations:")
        if status["applied"]:
            for ver, name, applied_at in status["applied"]:
                print(f"  [v{ver:04d}] {name} (applied: {applied_at})")
        else:
            print("  (none)")
        print("Pending migrations:")
        if status["pending"]:
            for ver, name, path, ext in status["pending"]:
                print(f"  [v{ver:04d}] {name} ({ext})")
        else:
            print("  (none - up to date)")
        return 0

    applied = run_migrations(db_path, migrations_dir=args.migrations_dir, backup_dir=args.backup_dir)
    if applied:
        print(f"Applied {len(applied)} migration(s) to {db_path}:")
        for ver, name in applied:
            print(f"  [v{ver:04d}] {name}")
    else:
        print(f"Database {db_path} is already up-to-date (v{get_current_version(db_path)}).")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    sys.exit(main())
