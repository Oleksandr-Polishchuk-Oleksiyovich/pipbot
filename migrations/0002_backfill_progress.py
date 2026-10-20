"""
0002_backfill_progress.py: Data migration from legacy progress table to user_progress.
"""
import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    # Check if progress table exists
    has_progress = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='progress'"
    ).fetchone()
    if not has_progress:
        return

    # Check if users table exists
    has_users = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if not has_users:
        return

    user_slugs = {row[0] for row in conn.execute("SELECT slug FROM users").fetchall()}

    # If users table has neither lyosha nor sasha (e.g. empty users table in legacy test),
    # insert baseline records for them so foreign keys are satisfied.
    if not user_slugs:
        conn.execute("""
            INSERT OR IGNORE INTO users (slug, display_name, status)
            VALUES ('lyosha', 'Льоша', 'active'), ('sasha', 'Саша', 'active')
        """)
        user_slugs = {"lyosha", "sasha"}

    user_cols = {row[1] for row in conn.execute("PRAGMA table_info(user_progress)").fetchall()}
    has_updated_at = "updated_at" in user_cols

    rows = conn.execute("SELECT date, lyosha_done, sasha_done FROM progress").fetchall()
    for date_iso, l_done, s_done in rows:
        for slug, done_flag in (("lyosha", l_done), ("sasha", s_done)):
            if slug in user_slugs:
                val = int(bool(done_flag))
                if has_updated_at:
                    conn.execute("""
                        INSERT INTO user_progress (date, user_slug, done, updated_at)
                        VALUES (?, ?, ?, datetime('now'))
                        ON CONFLICT(date, user_slug) DO UPDATE SET done = excluded.done, updated_at = datetime('now')
                    """, (date_iso, slug, val))
                else:
                    conn.execute("""
                        INSERT INTO user_progress (date, user_slug, done)
                        VALUES (?, ?, ?)
                        ON CONFLICT(date, user_slug) DO UPDATE SET done = excluded.done
                    """, (date_iso, slug, val))
