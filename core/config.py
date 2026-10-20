import os
from datetime import date

import pytz
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = int(os.getenv("CHAT_ID") or 0)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Шлях до БД: локально за замовчуванням у BASE_DIR, на сервері через env
# (наприклад, DB_PATH=/var/lib/sportnerdbot/challenge.db поза release-каталогом)
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "challenge.db"))
BACKUP_DIR = os.getenv("BACKUP_DIR", os.path.join(os.path.dirname(DB_PATH), "backups"))

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

