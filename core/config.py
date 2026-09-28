import os
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "8637170983:AAHctUodtAxnF8AzJZWBMW5H6k_X5AcrKeQ")
CHAT_ID = int(os.getenv("CHAT_ID", "-1004481392380"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
JEV_API_KEY = os.getenv("JEV_API_KEY", "")
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "challenge.db")

CANDIDATE_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]

