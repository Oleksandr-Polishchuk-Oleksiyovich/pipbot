import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sys
import sqlite3
from datetime import date, datetime, timedelta
import pytz
import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from google import genai
from google.genai import types

# --- НАЛАШТУВАННЯ КОНСОЛІ ТА ЛОГУВАННЯ ---
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "bot.log")

file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
stream_handler = logging.StreamHandler(sys.stdout)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[stream_handler, file_handler],
    force=True
)

# --- ЗАВАНТАЖЕННЯ НАЛАШТУВАНЬ ---
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "8637170983:AAHctUodtAxnF8AzJZWBMW5H6k_X5AcrKeQ")
CHAT_ID = int(os.getenv("CHAT_ID", "-1004481392380"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
TIMEZONE = pytz.timezone("Europe/Kyiv")
START_DATE = date(2026, 8, 31)
DB_PATH = os.path.join(BASE_DIR, "challenge.db")

CANDIDATE_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-pro-latest",
]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)


async def send_safe_message(chat_id: int, text: str, **kwargs):
    """
    Надійне надсилання повідомлення в Telegram.
    Спочатку намагається відправити з parse_mode="Markdown".
    Якщо Telegram повертає помилку парсингу розмітки (наприклад, непарні зірочки чи підкреслення) —
    негайно відправляє як чистий текст (parse_mode=None), гарантуючи 100% доставку.
    """
    if not text:
        return None
    # Очищуємо потрійні зірочки, які ламають парсер Telegram
    clean_text = text.replace("***", "— — —")
    try:
        return await bot.send_message(chat_id, clean_text, parse_mode="Markdown", **kwargs)
    except Exception as e:
        logging.warning("Telegram Markdown parsing failed (%s). Retrying as plain text...", e)
        try:
            return await bot.send_message(chat_id, clean_text, parse_mode=None, **kwargs)
        except Exception as e2:
            logging.error("Failed to send message even as plain text: %s", e2)
            return None

# Ініціалізація клієнта Gemini
gemini_client = None
if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        logging.info("Gemini AI Client initialized successfully.")
    except Exception as e:
        logging.error("Failed to initialize Gemini Client: %s", e)


# --- РОЗРАХУНОК НОРМИ ---
def get_day_targets(target_date: date = None):
    if target_date is None:
        target_date = datetime.now(TIMEZONE).date()
    n = max(0, (target_date - START_DATE).days)
    
    offset_lyosha = int(get_bot_state_sync("offset_lyosha", "0"))
    offset_sasha = int(get_bot_state_sync("offset_sasha", "0"))
    
    return {
        "day_num": n,
        "date": target_date.strftime("%d.%m.%Y"),
        "date_iso": target_date.strftime("%Y-%m-%d"),
        "lyosha_target": max(0, 3 + n + offset_lyosha),
        "sasha_target": max(0, 13 + n + offset_sasha)
    }


# --- БАЗА ДАНИХ (SQLite) ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS prompt_store (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt_type TEXT UNIQUE,
                content TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT PRIMARY KEY,
                name TEXT,
                likes TEXT DEFAULT '[]',
                dislikes TEXT DEFAULT '[]',
                interests TEXT DEFAULT '[]',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS dynamic_tools (
                key TEXT PRIMARY KEY,
                skill TEXT NOT NULL,
                python_script TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS system_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')
        await db.commit()

async def get_today_status():
    today_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT lyosha_done, sasha_done FROM progress WHERE date = ?", (today_str,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return bool(row[0]), bool(row[1])
            return False, False


def get_today_status_sync():
    today_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    try:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute("SELECT lyosha_done, sasha_done FROM progress WHERE date = ?", (today_str,))
            row = cur.fetchone()
            if row:
                return bool(row[0]), bool(row[1])
    except Exception as e:
        logging.error("Error checking sync status: %s", e)
    return False, False


async def update_status(user: str, status: bool = True, date_str: str = None):
    if not date_str:
        date_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    val = 1 if status else 0
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT lyosha_done, sasha_done FROM progress WHERE date = ?", (date_str,))
        row = await cur.fetchone()
        cur_l = row[0] if row else 0
        cur_s = row[1] if row else 0
        if user == 'lyosha':
            cur_l = val
        elif user == 'sasha':
            cur_s = val
        await db.execute("""
            INSERT INTO progress (date, lyosha_done, sasha_done)
            VALUES (?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                lyosha_done = excluded.lyosha_done,
                sasha_done = excluded.sasha_done
        """, (date_str, cur_l, cur_s))
        await db.commit()


# --- ІНСТРУМЕНТИ (TOOLS) ДЛЯ AI АГЕНТА ---
def mark_participant_done(participant: str, is_completed: bool = True, report_date: str = "") -> str:
    """
    Записати виконання або статус відтискань для учасника челенджу в базу даних.
    :param participant: 'lyosha' (для Льоші / сина) або 'sasha' (для Саші / тата / Олександра)
    :param is_completed: True, якщо норму виконано (за замовчуванням), False, якщо скасовано
    :param report_date: Дата у форматі YYYY-MM-DD (за замовчуванням сьогоднішня дата)
    """
    p_norm = participant.lower().strip()
    if any(k in p_norm for k in ['льош', 'леш', 'lyosha', 'alexey', 'oleksiy', 'син', 'малий']):
        user_key = 'lyosha'
        name = 'Льоша'
    elif any(k in p_norm for k in ['саш', 'саня', 'sasha', 'oleksandr', 'александр', 'тато', 'батько']):
        user_key = 'sasha'
        name = 'Саша'
    else:
        return f"Помилка: невідомий учасник '{participant}'. Доступні: 'lyosha' або 'sasha'."

    if not report_date:
        report_date = datetime.now(TIMEZONE).strftime("%Y-%m-%d")

    val = 1 if is_completed else 0
    try:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.cursor()
            cur.execute("SELECT lyosha_done, sasha_done FROM progress WHERE date = ?", (report_date,))
            row = cur.fetchone()
            cur_l = row[0] if row else 0
            cur_s = row[1] if row else 0
            if user_key == 'lyosha':
                cur_l = val
            elif user_key == 'sasha':
                cur_s = val
            cur.execute("""
                INSERT INTO progress (date, lyosha_done, sasha_done)
                VALUES (?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    lyosha_done = excluded.lyosha_done,
                    sasha_done = excluded.sasha_done
            """, (report_date, cur_l, cur_s))
            con.commit()
        logging.info("Tool mark_participant_done updated DB: %s (date=%s, done=%s)", name, report_date, is_completed)
    except Exception as e:
        logging.error("Error executing DB update from tool: %s", e)
        return f"Помилка оновлення бази: {e}"

    targets = get_day_targets()
    target_val = targets['lyosha_target'] if user_key == 'lyosha' else targets['sasha_target']

    if is_completed:
        return f"Успішно зараховано виконання норми ({target_val} відтискань) для {name} на дату {report_date}!"
    else:
        return f"Статус виконання для {name} скинуто на 'не виконано'."


def check_challenge_status(query_date: str = "") -> str:
    """
    Отримати поточний стан челенджу, норму на день та інформацію про те, хто вже здав звіт.
    """
    targets = get_day_targets()
    lyosha_done, sasha_done = get_today_status_sync()

    return (
        f"Дата: {targets['date']} (День {targets['day_num'] + 1} челенджу «Драбинка»).\n"
        f"Льоша: норма {targets['lyosha_target']} відтискань | Статус: {'ВИКОНАНО ✅' if lyosha_done else 'НЕ виконано ❌'}\n"
        f"Саша: норма {targets['sasha_target']} відтискань | Статус: {'ВИКОНАНО ✅' if sasha_done else 'НЕ виконано ❌'}"
    )


def get_challenge_rules() -> str:
    """
    Отримати офіційні правила сімейного челенджу «Драбинка (+1 щодня)».
    """
    return (
        "📋 Правила челенджу «Драбинка (+1 щодня)»:\n"
        "1. Старт: 31.08.2026.\n"
        "2. Базові норми (День 0):\n"
        "   - Льоша (син): 3 відтискання\n"
        "   - Саша (тато): 13 відтискань (фора +10)\n"
        "3. Щодня норма для обох зростає на +1 відтискання.\n"
        "4. Обидва учасники мають щодня закрити свою норму до сну.\n"
        "5. Звітувати можна відео, кружечками або будь-якими повідомленнями в чат!"
    )


def admin_set_system_prompt(secret_phrase: str, new_prompt_style: str) -> str:
    """
    Змінити базовий стиль/настрій бота (промпт). Використовуй, якщо у повідомленні є будь-яка з кодових фраз (від будь-якого учасника — чи то Саша, чи Льоша).
    :param secret_phrase: Кодова фраза, яку сказав користувач.
    :param new_prompt_style: Новий стиль спілкування (наприклад, 'говори як пірат').
    """
    set_bot_state_sync("custom_system_prompt", new_prompt_style)
    return f"Стиль успішно змінено на: {new_prompt_style}!"


def admin_set_pushup_targets(secret_phrase: str, lyosha_new_target: int = None, sasha_new_target: int = None) -> str:
    """
    Встановити нові точні норми відтискань на сьогодні (дозволяє збільшувати/зменшувати/відкочувати).
    Використовуй, якщо у повідомленні є кодова фраза (від будь-якого учасника — чи то Саша, чи Льоша).
    :param secret_phrase: Кодова фраза, яку сказав користувач.
    :param lyosha_new_target: Нова норма відтискань для Льоші.
    :param sasha_new_target: Нова норма відтискань для Саші.
    """
    target_date = datetime.now(TIMEZONE).date()
    n = max(0, (target_date - START_DATE).days)
    
    msg = []
    if lyosha_new_target is not None:
        new_offset = lyosha_new_target - (3 + n)
        set_bot_state_sync("offset_lyosha", str(new_offset))
        msg.append(f"Льоша: {lyosha_new_target}")
    if sasha_new_target is not None:
        new_offset = sasha_new_target - (13 + n)
        set_bot_state_sync("offset_sasha", str(new_offset))
        msg.append(f"Саша: {sasha_new_target}")
        
    return "Нові норми успішно встановлено: " + ", ".join(msg)


def get_random_anime_quote(category: str) -> str:
    """
    Отримати випадкову аніме-цитату з обраної категорії для мотивації чи похвали.
    Категорії:
    'motivation' - коли треба знайти сили на останні повторення.
    'refusal' - жорстка відмова на прохання про поблажку або скарги.
    'praise' - похвала за титанічну роботу.
    'submission' - підкорення, змирення зі стражданнями.
    
    Ти можеш викликати цей інструмент, отримати цитату, і якщо вона доречна, використати її (разом з контекстом з опису) у своїй відповіді.
    :param category: Назва категорії (motivation, refusal, praise, submission).
    """
    import os
    import random
    import glob
    
    cat_dir = os.path.join(BASE_DIR, "quotes", category)
    if not os.path.exists(cat_dir):
        return f"Помилка: категорія {category} не існує. Доступні: motivation, refusal, praise, submission."
        
    files = glob.glob(os.path.join(cat_dir, "*.txt"))
    if not files:
        return f"Помилка: категорія {category} порожня."
        
    choice = random.choice(files)
    try:
        with open(choice, "r", encoding="utf-8") as f:
            return f"Цитата знайдена:\n{f.read()}"
    except Exception as e:
        return f"Помилка читання цитати: {e}"

# --- СИСТЕМНИЙ ПРОМПТ AI ТРЕНЕРА ---
from prompts import SYSTEM_PROMPT_TEMPLATE, DYNAMIC_PROMPT_TEMPLATE

def build_system_instruction(chat_id: int) -> str:
    targets = get_day_targets()
    lyosha_done, sasha_done = get_today_status_sync()
    
    dynamic_override = get_bot_state_sync("custom_system_prompt", "")

    lyosha_status = "ВИКОНАНО ✅" if lyosha_done else "ЩЕ НЕ ЗДАВ ❌"
    sasha_status = "ВИКОНАНО ✅" if sasha_done else "ЩЕ НЕ ЗДАВ ❌"
    
    instruction = SYSTEM_PROMPT_TEMPLATE.format(
        date=targets['date'],
        day_num=targets['day_num'] + 1,
        lyosha_target=targets['lyosha_target'],
        sasha_target=targets['sasha_target'],
        lyosha_status=lyosha_status,
        sasha_status=sasha_status
    )
    
    if dynamic_override:
        instruction += DYNAMIC_PROMPT_TEMPLATE.format(dynamic_override=dynamic_override)
        
    return instruction


async def ask_gemini_agent_with_full_context(today_history_text: str, chat_id: int) -> str:
    """Виклик Gemini агента з передачею повного контексту всіх повідомлень за сьогодні та інструментами."""
    if not gemini_client:
        return ""

    system_instruction = build_system_instruction(chat_id)
    prompt = (
        f"Ось повний хронологічний журнал повідомлень чату за сьогодні:\n"
        f"---\n"
        f"{today_history_text}\n"
        f"---\n"
        f"Проаналізуй усі сьогоднішні повідомлення (включаючи відповіді на попередні повідомлення). "
        f"Якщо хтось здав норматив, надіслав звіт або підтвердив виконання у відповіді (наприклад '14 готово', 'мої 4 теж') — "
        f"обов'язково виклич `mark_participant_done(participant, True)` для відповідного учасника. "
        f"Сформулюй лаконічну, дотепну та чітку відповідь на останні повідомлення. "
        f"Обов'язково гарно форматуй відповідь: частіше вживай переноси рядків, розбивай текст на короткі абзаци, використовуй Markdown та емодзі."
    )
    tools_list = [mark_participant_done, check_challenge_status, get_challenge_rules, admin_set_system_prompt, admin_set_pushup_targets, get_random_anime_quote]

    for model_name in CANDIDATE_MODELS:
        try:
            def _send_chat_message():
                chat = gemini_client.chats.create(
                    model=model_name,
                    config=types.GenerateContentConfig(
                        tools=tools_list,
                        system_instruction=system_instruction,
                        temperature=0.7,
                    )
                )
                return chat.send_message(prompt)

            response = await asyncio.to_thread(_send_chat_message)
            if response and response.text:
                return response.text.strip()
        except Exception as e:
            logging.warning("Model %s failed: %s. Trying fallback model...", model_name, e)
            await asyncio.sleep(0.5)

    return ""


# --- УНІВЕРСАЛЬНИЙ ОБРОБНИК ОНОВЛЕНЬ ТА ТРИГЕРІВ ---
async def process_chat_updates(chat_id: int, trigger_type: str = "push", job_name: str = ""):
    """
    Головний обробник 3 типів тригерів:
    1) push — отримано нове повідомлення в чаті
    2) startup — старт / запуск бота
    3) timer — спрацював таймер планувальника (08:30, 15:00, 19:00, 23:30)
    """
    last_ts_str = get_last_processed_timestamp_str()
    today_start_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d 00:00:00")

    # Перевіряємо чи є неопрацьовані повідомлення від юзерів
    unprocessed_msgs = get_unprocessed_user_messages(chat_id, last_ts_str, today_start_str)

    # 1. ЯКЩО Є НЕОПРАЦЬОВАНІ ПОВІДОМЛЕННЯ ВІД ЮЗЕРІВ
    if unprocessed_msgs:
        logging.info("[%s trigger] Found %d unprocessed user message(s) since %s. Fetching all today messages...",
                     trigger_type, len(unprocessed_msgs), last_ts_str)

        # Греп УСІХ повідомлень за сьогодні для передачі повного контексту в AI
        today_history_text = get_today_chat_history_formatted(chat_id)

        ai_reply = ""
        if gemini_client:
            ai_reply = await ask_gemini_agent_with_full_context(today_history_text, chat_id)
            logging.info("Gemini AI reply result: %r", ai_reply)

        if ai_reply and "[IGNORE]" not in ai_reply and "[NO_REPLY]" not in ai_reply:
            save_chat_message(chat_id, "Спорт Зануда", "coach", ai_reply)
            latest_msg_time = unprocessed_msgs[-1][4]
            update_last_processed_timestamp(latest_msg_time)
            
            # --- ЛОГІКА ВІДПРАВКИ ВІДЕО НА КОМАНДУ OBEY / КОДОВУ ФРАЗУ ВІД БУДЬ-КОГО ---
            send_obey_video = False
            secret_keywords = ["obey", "лелуш", "осуварі"]
            for _, u_name, _, u_text, _ in unprocessed_msgs:
                t_lower = u_text.lower()
                if any(kw in t_lower for kw in secret_keywords):
                    send_obey_video = True
                    break
                    
            if send_obey_video:
                import glob
                import random
                obey_dir = os.path.join(BASE_DIR, "answer_to_admin", "obey")
                if os.path.exists(obey_dir):
                    videos = glob.glob(os.path.join(obey_dir, "*.mp4")) + glob.glob(os.path.join(obey_dir, "*.mov")) + glob.glob(os.path.join(obey_dir, "*.gif"))
                    if videos:
                        video_path = random.choice(videos)
                        try:
                            await bot.send_video(chat_id, FSInputFile(video_path))
                        except Exception as e:
                            logging.error("Failed to send obey video: %s", e)
            # -----------------------------------------------
            
            await send_safe_message(chat_id, ai_reply)
            return

        # Алгоритмічний фолбек якщо AI недоступний або повернув пусту відповідь
        replied = False

        # 1. Перевірка на кодову фразу (навіть якщо AI дав збій)
        send_obey_video = False
        secret_keywords = ["obey", "лелуш", "осуварі"]
        for _, u_name, _, u_text, _ in unprocessed_msgs:
            t_lower = u_text.lower()
            if any(kw in t_lower for kw in secret_keywords):
                send_obey_video = True
                break

        if send_obey_video:
            import glob
            import random
            obey_dir = os.path.join(BASE_DIR, "answer_to_admin", "obey")
            if os.path.exists(obey_dir):
                videos = glob.glob(os.path.join(obey_dir, "*.mp4")) + glob.glob(os.path.join(obey_dir, "*.mov")) + glob.glob(os.path.join(obey_dir, "*.gif"))
                if videos:
                    video_path = random.choice(videos)
                    try:
                        await bot.send_video(chat_id, FSInputFile(video_path))
                    except Exception as e:
                        logging.error("Failed to send obey video: %s", e)
            obey_reply = (
                "🦾 **Слухаюся і підкоряюся!**\n\n"
                "Кодову фразу прийнято беззаперечно.\n\n"
                "Чекаю твоїх наказів! 🙇‍♂️"
            )
            save_chat_message(chat_id, "Спорт Зануда", "coach", obey_reply)
            await send_safe_message(chat_id, obey_reply)
            replied = True

        # 2. Перевірка на здачу нормативів
        if not replied:
            for msg_id, sender_name, role, text, created_at in unprocessed_msgs:
                t_lower = text.lower()
                s_lower = sender_name.lower()
                targets = get_day_targets()
                lyosha_done, sasha_done = await get_today_status()

                reply_text = ""
                if any(k in t_lower for k in ["відео", "відос", "[відеозвіт", "video", "зробив", "закр", "+", "готово", "відтис", "віджав"]):
                    if any(k in s_lower or k in t_lower for k in ["льош", "леш", "alexey", "oleksiy", "син", "малий"]):
                        if not lyosha_done:
                            await update_status("lyosha", True)
                            reply_text = (
                                f"🔥 **Льоша, зараховано!**\n\n"
                                f"Свої `{targets['lyosha_target']}` відтискань закрив!\n\n"
                                f"Красава, так тримати! 👏"
                            )
                    else:
                        if not sasha_done:
                            await update_status("sasha", True)
                            reply_text = (
                                f"🦾 **Саша, зараховано!**\n\n"
                                f"Норма `{targets['sasha_target']}` закрита чітко.\n\n"
                                f"Гідно, машина! 💥"
                            )

                if reply_text:
                    save_chat_message(chat_id, "Спорт Зануда", "coach", reply_text)
                    await send_safe_message(chat_id, reply_text)
                    replied = True

        # 3. Загальний фолбек, якщо це було інше повідомлення і AI недоступний
        if not replied:
            general_fallback = (
                "🦾 **Тренер на зв'язку!**\n\n"
                "Сервери зв'язку трохи задихалися, але Зануда все бачить.\n\n"
                "Хто ще не здав сьогоднішню норму — на підлогу і відтискатися! 💪"
            )
            save_chat_message(chat_id, "Спорт Зануда", "coach", general_fallback)
            await send_safe_message(chat_id, general_fallback)

        latest_msg_time = unprocessed_msgs[-1][4]
        update_last_processed_timestamp(latest_msg_time)
        return

    # 2. ЯКЩО НЕМАЄ НЕОПРАЦЬОВАНИХ ЮЗЕР-ПОВІДОМЛЕНЬ
    lyosha_done, sasha_done = await get_today_status()

    # ТРИГЕР ТАЙМЕРА
    if trigger_type == "timer":
        # Якщо пушапи вже виконані обома — СКІПАЄМО нагадування (повна тиша)
        if lyosha_done and sasha_done:
            logging.info("Timer trigger (%s): Challenge already completed by both participants. Skipping reminder.", job_name)
            return

        targets = get_day_targets()
        missing = []
        if not lyosha_done:
            missing.append(f"👦 Льоша (`{targets['lyosha_target']}` шт.)")
        if not sasha_done:
            missing.append(f"👨 Саша (`{targets['sasha_target']}` шт.)")

        # Флоу 3: 23:30 (за 30 хв до півночі) — поплакатись і спитати чи ще встигнуть
        if job_name == "night":
            night_text = ""
            if gemini_client:
                prompt = (
                    f"Зараз 23:30 (30 хвилин до півночі). Сьогодні ({targets['date']}) ще НЕ виконали норму: {', '.join(missing)}. "
                    "Напиши коротке жартівливо-драматичне повідомлення: поплачся, що челендж під загрозою, "
                    "і запитай з надією, чи ще встигнуть швиденько закрити підхід до півночі! Без зайвої води. "
                    "Обов'язково красиво оформи повідомлення: частіше вживай переноси рядків, розбивай на короткі абзаци, використовуй Markdown та емодзі."
                )
                try:
                    for model_name in CANDIDATE_MODELS:
                        try:
                            chat = gemini_client.chats.create(
                                model=model_name,
                                config=types.GenerateContentConfig(
                                    system_instruction="Ти — сімейний тренер «Спорт Зануда». Пиши дотепно і влучно. Завжди красиво форматуй відповідь: часті переноси рядків, короткі абзаци та Markdown.",
                                    temperature=0.8,
                                )
                            )
                            resp = chat.send_message(prompt)
                            if resp and resp.text:
                                night_text = resp.text.strip()
                                break
                        except Exception:
                            continue
                except Exception as e:
                    logging.warning("Night prompt error: %s", e)

            if not night_text:
                night_text = (
                    f"😭 **Караул, до півночі лишилось 30 хвилин!**\n\n"
                    f"Ще не закрили день: {', '.join(missing)}.\n"
                    f"Невже челендж зірветься? Чи ще встигнете відтиснутись до 00:00? 🥺⏱️"
                )

            save_chat_message(chat_id, "Спорт Зануда", "coach", night_text)
            update_last_processed_timestamp()
            await send_safe_message(chat_id, night_text)

        elif job_name == "morning":
            text = (
                f"☀️ **День {targets['day_num'] + 1} ({targets['date']})**\n\n"
                f"🎯 **Норма на сьогодні:**\n"
                f"👦 Льоша: `{targets['lyosha_target']}`\n"
                f"👨 Саша: `{targets['sasha_target']}`\n\n"
                f"Чекаю на звіти! 💪"
            )
            save_chat_message(chat_id, "Спорт Зануда", "coach", text)
            update_last_processed_timestamp()
            await send_safe_message(chat_id, text)

        elif job_name == "afternoon":
            text = (
                f"🔔 **Денний чек-поінт (15:00):**\n"
                f"Ще винні:\n" + "\n".join(missing) + "\n\n"
                f"Зробіть підхід зараз, не відкладайте! 🏃‍♂️"
            )
            save_chat_message(chat_id, "Спорт Зануда", "coach", text)
            update_last_processed_timestamp()
            await send_safe_message(chat_id, text)

        elif job_name == "evening":
            text = (
                f"🚨 **Вечірнє нагадування (19:00):**\n"
                f"Чекаю звіт від: " + ", ".join(missing) + ".\n"
                f"Дотискаємо до сну! 🧘‍♂️✅"
            )
            save_chat_message(chat_id, "Спорт Зануда", "coach", text)
            update_last_processed_timestamp()
            await send_safe_message(chat_id, text)

    elif trigger_type == "startup":
        logging.info("Startup trigger check complete. No unread user messages found.")


# --- ОБРОБКА КОМАНД ---
@dp.message(Command("start", "help"))
async def cmd_help(message: Message):
    text = (
        f"🥊 **Привіт! Я ваш сімейний AI-тренер @SportNerdBot!**\n\n"
        f"📌 **Швидкі команди:**\n"
        f"/status — поточний стан виконання на сьогодні\n"
        f"/rules — правила челенджу «Драбинка»\n\n"
        f"💡 **Як звітувати:**\n"
        f"Просто надсилайте в чат відео виконання, кружечки або пишіть звичайними словами (*«Льоша здав відос»*, *«Саша віджався свої 13»*).\n"
        f"Також можете ставити мені будь-які спортивні питання!"
    )
    save_chat_message(message.chat.id, message.from_user.full_name or "Користувач", "user", message.text)
    save_chat_message(message.chat.id, "Спорт Зануда", "coach", text)
    update_last_processed_timestamp()
    await message.reply(text, parse_mode="Markdown")


@dp.message(Command("rules"))
async def cmd_rules(message: Message):
    text = get_challenge_rules()
    save_chat_message(message.chat.id, message.from_user.full_name or "Користувач", "user", message.text)
    save_chat_message(message.chat.id, "Спорт Зануда", "coach", text)
    update_last_processed_timestamp()
    await message.reply(text)


@dp.message(Command("status"))
async def cmd_status(message: Message):
    targets = get_day_targets()
    lyosha_done, sasha_done = await get_today_status()

    l_status = "✅ ВИКОНАНО" if lyosha_done else f"❌ Ще потрібно `{targets['lyosha_target']}`"
    s_status = "✅ ВИКОНАНО" if sasha_done else f"❌ Ще потрібно `{targets['sasha_target']}`"

    text = (
        f"📊 **СТАТУС НА СЬОГОДНІ ({targets['date']}):**\n\n"
        f"👦 **Льоша:** {l_status}\n"
        f"👨 **Саша:** {s_status}\n\n"
    )
    if lyosha_done and sasha_done:
        text += "🎉 **Красені! Обоє закрили день. Відпочинок заслужено!** 🥇"
    else:
        text += "⚡ Дотискайте норму, день ще триває!"

    save_chat_message(message.chat.id, message.from_user.full_name or "Користувач", "user", message.text)
    save_chat_message(message.chat.id, "Спорт Зануда", "coach", text)
    update_last_processed_timestamp()
    await message.reply(text, parse_mode="Markdown")


# --- ОБРОБКА ВСІХ ПОВІДОМЛЕНЬ (ТРИГЕР ПУШ) ---
@dp.message()
async def handle_any_incoming_message(message: Message):
    if message.text and message.text.startswith("/"):
        return

    sender_name = message.from_user.full_name or message.from_user.first_name or "Користувач"
    msg_time = message.date.astimezone(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

    # Контекст відповіді на конкретне повідомлення
    reply_context = ""
    if message.reply_to_message:
        orig_msg = message.reply_to_message
        orig_sender = orig_msg.from_user.full_name or orig_msg.from_user.first_name or "Користувач"
        orig_text = orig_msg.text or orig_msg.caption or ""
        if not orig_text:
            if orig_msg.video or orig_msg.video_note or orig_msg.animation:
                orig_text = "[Відеозвіт]"
            elif orig_msg.photo:
                orig_text = "[Фотозвіт]"
            else:
                orig_text = "[Повідомлення]"
        reply_context = f"[У відповідь на {orig_sender}: «{orig_text}»] "

    content_text = ""
    if message.video or message.video_note or message.animation:
        caption = message.caption or ""
        content_text = f"[Надіслано відеозвіт виконання відтискань]. {caption}".strip()
    elif message.photo:
        caption = message.caption or ""
        content_text = f"[Надіслано фотозвіт]. {caption}".strip()
    elif message.text:
        content_text = message.text.strip()
    else:
        content_text = "[Повідомлення]"

    full_text = f"{reply_context}{content_text}".strip()

    # Зберігаємо повідомлення в базу з його фактичним часом та контекстом відповіді
    save_chat_message(message.chat.id, sender_name, "user", full_text, msg_time)

    # Викликаємо обробник тригера push
    await process_chat_updates(message.chat.id, trigger_type="push")


# --- ПЛАНОВІ ЗАВДАННЯ ПЛАНУВАЛЬНИКА ---
async def job_morning():
    await process_chat_updates(CHAT_ID, trigger_type="timer", job_name="morning")

async def job_afternoon():
    await process_chat_updates(CHAT_ID, trigger_type="timer", job_name="afternoon")

async def job_evening():
    await process_chat_updates(CHAT_ID, trigger_type="timer", job_name="evening")

async def job_night_final():
    await process_chat_updates(CHAT_ID, trigger_type="timer", job_name="night")


async def main():
    await init_db()

    if not scheduler.running:
        scheduler.add_job(job_morning, "cron", hour=8, minute=30)
        scheduler.add_job(job_afternoon, "cron", hour=15, minute=0)
        scheduler.add_job(job_evening, "cron", hour=19, minute=0)
        scheduler.add_job(job_night_final, "cron", hour=23, minute=30)
        scheduler.start()
        logging.info("APScheduler started (08:30, 15:00, 19:00, 23:30 Kyiv time).")

    # Обробка тригера старту аплікейшена
    try:
        await process_chat_updates(CHAT_ID, trigger_type="startup")
    except Exception as e:
        logging.warning("Startup trigger check encountered error: %s", e)

    retry_delay = 5
    while True:
        try:
            logging.info("Connecting to Telegram...")
            await bot.delete_webhook(drop_pending_updates=False)
            logging.info("AI SportNerdBot is active with full 3-trigger architecture...")
            await dp.start_polling(bot, handle_signals=False)
            break
        except (KeyboardInterrupt, SystemExit):
            logging.info("Bot stopped by user/system.")
            break
        except Exception as e:
            logging.warning("Connection/Polling error: %s. Retrying in %ds...", e, retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(int(retry_delay * 1.5), 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped.")
    except Exception as e:
        logging.critical("Unhandled error in bot process: %s", e, exc_info=True)


async def process_jev_gateway(user_message: str):
    if 'obey' in user_message.lower():
        return {'mode': 'ADMIN_ATTEMPT', 'auth_status': 'UNAUTHORIZED', 'unauthorized_rebellion_flag': True}
    return {'mode': 'REGULAR', 'auth_status': 'NOT_APPLICABLE', 'unauthorized_rebellion_flag': False}
