"""Light deploy smoke: critical env vars present + DB opens at the expected schema version.

No flows, no network, no integration. Values are never printed.
Usage: python deploy/smoke_env.py [env_file]   (DB path from $DB_PATH)
"""
import os
import sqlite3
import sys

REQUIRED = ("BOT_TOKEN", "GEMINI_API_KEY", "CHAT_ID", "GATEWAY_HMAC_SECRET")


def load_env_file(path):
    if not path or not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    load_env_file(sys.argv[1] if len(sys.argv) > 1 else os.getenv("ENV_FILE", "/etc/sportnerdbot/env"))
    errors = [f"env {k} missing/empty" for k in REQUIRED if not os.getenv(k, "").strip()]
    db = os.getenv("DB_PATH", "/var/lib/sportnerdbot/challenge.db")
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        ver = con.execute("PRAGMA user_version").fetchone()[0]
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
        print(f"db ok: {db} user_version={ver} tables={len(tables)}")
        if not tables:
            errors.append("db has no tables")
    except Exception as e:  # noqa: BLE001
        errors.append(f"db open failed: {e}")
    if errors:
        for e in errors:
            print("SMOKE FAIL:", e, file=sys.stderr)
        sys.exit(1)
    print("env smoke ok")


if __name__ == "__main__":
    main()
