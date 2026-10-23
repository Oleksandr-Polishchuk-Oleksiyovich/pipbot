import os
from datetime import date

import pytz
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Продакшн-індикатор: явний APP_ENV=production АБО код запущено з release-каталогу деплою
# (/opt/sportnerdbot/releases/<sha> чи symlink /opt/sportnerdbot/current — див. deploy/deploy.sh).
_RELEASE_DIR_PREFIX = os.path.join(os.sep, "opt", "sportnerdbot")
IS_PRODUCTION = (
    os.getenv("APP_ENV", "").strip().lower() == "production"
    or BASE_DIR == _RELEASE_DIR_PREFIX.rstrip(os.sep)
    or BASE_DIR.startswith(_RELEASE_DIR_PREFIX + os.sep)
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = int(os.getenv("CHAT_ID") or 0)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Шлях до БД: локально за замовчуванням у BASE_DIR, на сервері — лише явно через env
# (наприклад, DB_PATH=/var/lib/sportnerdbot/challenge.db поза release-каталогом).
# У продакшні (IS_PRODUCTION) DB_PATH МАЄ бути заданий явно і НЕ може лежати всередині BASE_DIR
# (release/symlink-каталог деплою) — інакше дані губляться при кожному новому релізі/prune.
_env_db_path = os.getenv("DB_PATH", "").strip()
if IS_PRODUCTION:
    if not _env_db_path:
        raise RuntimeError(
            "DB_PATH is not set in production (APP_ENV=production or running under "
            f"{_RELEASE_DIR_PREFIX}) — refusing to fall back to a path inside the release "
            "directory. Set DB_PATH explicitly (e.g. /var/lib/sportnerdbot/challenge.db) in "
            "/etc/sportnerdbot/env."
        )
    DB_PATH = os.path.abspath(_env_db_path)
    _base_abs = os.path.abspath(BASE_DIR)
    if DB_PATH == _base_abs or DB_PATH.startswith(_base_abs + os.sep):
        raise RuntimeError(
            f"DB_PATH ({DB_PATH}) resolves inside the code/release directory ({_base_abs}) — "
            "this would lose the database on every new deploy. Point DB_PATH to a persistent "
            "location outside the release directory (e.g. /var/lib/sportnerdbot/challenge.db)."
        )
else:
    DB_PATH = os.path.abspath(_env_db_path) if _env_db_path else os.path.join(BASE_DIR, "challenge.db")

BACKUP_DIR = os.getenv("BACKUP_DIR", os.path.join(os.path.dirname(DB_PATH), "backups"))

# Скільки днів тримати "гучні" лог-дані (cv_jobs, offload_requests)
# та файлові/journald логи. admin_audit і prompt_history НЕ чистяться (аудит/історія версій).
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "7"))

# sportnerd-gateway (aiohttp) конфігурація
GATEWAY_HMAC_SECRET = os.getenv("GATEWAY_HMAC_SECRET", "")
GATEWAY_HOST = os.getenv("GATEWAY_HOST", "127.0.0.1")
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8443"))
DEPLOY_SCRIPT_PATH = os.getenv("DEPLOY_SCRIPT_PATH", os.path.join(BASE_DIR, "deploy", "deploy.sh"))
DEPLOY_STATUS_DIR = os.getenv("DEPLOY_STATUS_DIR", os.path.join(os.path.dirname(DB_PATH), "deploy_status"))

# CV Worker конфігурація (Phase 3 / mediapipe_limits Sequential benchmark)
CV_TARGET_FPS = int(os.getenv("CV_TARGET_FPS", "15"))
CV_MAX_SIDE = int(os.getenv("CV_MAX_SIDE", "480"))
CV_MODEL = os.getenv("CV_MODEL", "lite")

# Box CV offload (gateway long-poll /cv/*). CV_OFFLOAD=1: VM не рахує CV сам, а ставить cv_jobs для box worker-а.
CV_OFFLOAD = os.getenv("CV_OFFLOAD", "1") == "1"
CV_CLAIM_WAIT_S = float(os.getenv("CV_CLAIM_WAIT_S", "25"))
CV_RESULTS_INTERVAL_SEC = int(os.getenv("CV_RESULTS_INTERVAL_SEC", "10"))
ADMIN_TASKS_ANNOUNCE_INTERVAL_SEC = int(os.getenv("ADMIN_TASKS_ANNOUNCE_INTERVAL_SEC", "60"))
CV_LEASE_S = int(os.getenv("CV_LEASE_S", "300"))
CV_VIDEO_DIR = os.getenv("CV_VIDEO_DIR", os.path.join(os.path.dirname(DB_PATH), "cv_videos"))
CV_STILL_COUNTING_MIN = int(os.getenv("CV_STILL_COUNTING_MIN", "5"))

# Grok Bot box routine webhook offload конфігурація
ROUTINE_WEBHOOK_URL = os.getenv("ROUTINE_WEBHOOK_URL", "")
ROUTINE_SENDER_KEY = os.getenv("ROUTINE_SENDER_KEY", "")
ROUTINE_SHARED_SECRET = os.getenv("ROUTINE_SHARED_SECRET", "")
ROUTINE_TIMEOUT_SECONDS = int(os.getenv("ROUTINE_TIMEOUT_SECONDS", "900"))  # 15 хвилин за замовчуванням

TIMEZONE = pytz.timezone("Europe/Kyiv")
START_DATE = date(2026, 8, 31)

QUOTES_DIR = os.path.join(BASE_DIR, "quotes")
OBEY_MEDIA_DIR = os.path.join(BASE_DIR, "answer_to_admin", "obey")

# Порядок = пріоритет. Перша модель, що відповіла, піднімається нагору списку.
CANDIDATE_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
]

# Маркери кодових фраз адміна (нижній регістр, підрядки). Ловлять і варіації написання:
# «Лелуш/ЛІЛУШ ві Британія», «Obey me / You will obey», «Осуварі».
ADMIN_PASSPHRASE_MARKERS = [
    "obey",
    "лелуш",
    "лілуш",
    "лелуч",
    "lelouch",
    "британія наказує",
    "британия приказывает",
    "осуварі",
    "осувари",
]

# Guardrails та allowlist адмінів (T-52):
# Список дозволених telegram_id адмінів. Якщо порожній, дозволяє доступ за наявності passphrase.
ADMIN_TELEGRAM_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",")
    if x.strip().isdigit()
]
ADMIN_DEV_RATE_LIMIT_HOUR = int(os.getenv("ADMIN_DEV_RATE_LIMIT_HOUR", "5"))

ADMIN_TASK_STALE_HOURS = float(os.getenv("ADMIN_TASK_STALE_HOURS", "24"))
