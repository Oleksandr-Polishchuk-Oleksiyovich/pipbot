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
