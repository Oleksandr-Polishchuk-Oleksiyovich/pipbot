import os
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "8637170983:AAHctUodtAxnF8AzJZWBMW5H6k_X5AcrKeQ")
CHAT_ID = int(os.getenv("CHAT_ID", "-1004481392380"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
JEV_API_KEY = os.getenv("JEV_API_KEY", "")
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "challenge.db")
