"""
0013_video_fingerprints.py: метадані отримання відео (розмір, розміри кадру, тривалість, ім'я), sha256 байтів і
відбиток від box CV (хеші кадрів + таймінги повторів) — для дедуплікації повторно завантажених відео.
Зберігаємо ~3 доби (чиститься при вставці).
"""
import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS video_fingerprints (
            request_id  TEXT PRIMARY KEY,
            user_slug   TEXT,
            file_unique_id TEXT,
            file_size   INTEGER,
            width       INTEGER,
            height      INTEGER,
            duration_s  REAL,
            file_name   TEXT,
            sha256      TEXT,
            sha_bytes   INTEGER,
            fingerprint TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_video_fp_user ON video_fingerprints(user_slug, created_at);
    """)
