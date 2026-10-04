"""Telegram-адаптер (aiogram) + планувальник. Уся логіка — в app/flow.py та agents/."""
import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.flow import ChatFlow
from core import challenge
from core.config import BASE_DIR, BOT_TOKEN, CANDIDATE_MODELS, CHAT_ID, DB_PATH, GEMINI_API_KEY, TIMEZONE
from core.llm import GeminiLLM
from database.store import Store


def setup_logging() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            RotatingFileHandler(os.path.join(BASE_DIR, "bot.log"), maxBytes=5 * 1024 * 1024,
                                backupCount=3, encoding="utf-8"),
        ],
        force=True,
    )


class TelegramSender:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def send_text(self, chat_id: int, text: str) -> None:
        """Спершу Markdown; якщо Telegram не розпарсив розмітку — відправляємо чистим текстом."""
        if not text:
            return
        clean = text.replace("***", "— — —")
        try:
            await self.bot.send_message(chat_id, clean, parse_mode="Markdown")
        except Exception as e:
            logging.warning("Markdown send failed (%s), retrying as plain text", e)
            await self.bot.send_message(chat_id, clean, parse_mode=None)

    async def send_video(self, chat_id: int, path: str) -> None:
        await self.bot.send_video(chat_id, FSInputFile(path))


def describe_message(message: Message) -> str:
    """Текстове представлення повідомлення для журналу (медіа, підписи, контекст reply)."""
    reply_context = ""
    if message.reply_to_message:
        orig = message.reply_to_message
        orig_sender = (orig.from_user.full_name or orig.from_user.first_name) if orig.from_user else "Користувач"
        orig_text = orig.text or orig.caption or ""
        if not orig_text:
            if orig.video or orig.video_note or orig.animation:
                orig_text = "[Відеозвіт]"
            elif orig.photo:
                orig_text = "[Фотозвіт]"
            else:
                orig_text = "[Повідомлення]"
        reply_context = f"[У відповідь на {orig_sender}: «{orig_text}»] "

    if message.video or message.video_note or message.animation:
        content = f"[Надіслано відеозвіт виконання норми]. {message.caption or ''}".strip()
    elif message.photo:
        content = f"[Надіслано фотозвіт]. {message.caption or ''}".strip()
    elif message.text:
        content = message.text.strip()
    else:
        content = "[Повідомлення]"
    return f"{reply_context}{content}".strip()


def build_app():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("BOT_TOKEN and CHAT_ID must be set in .env")
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    store = Store(DB_PATH)
    llm = GeminiLLM.from_api_key(GEMINI_API_KEY, CANDIDATE_MODELS) if GEMINI_API_KEY else None
    if llm is None:
        logging.error("GEMINI_API_KEY is empty — bot will run on deterministic fallbacks only")
    flow = ChatFlow(store, llm, TelegramSender(bot))

    async def reply_command(message: Message, text: str, markdown: bool = True) -> None:
        name = message.from_user.full_name or "Користувач"
        flow.record_user_message(message.chat.id, name, message.text or "")
        store.save_message(message.chat.id, "Спорт Зануда", "coach", text,
                           flow.now().strftime("%Y-%m-%d %H:%M:%S"))
        store.set_state("last_processed_timestamp", flow.now().strftime("%Y-%m-%d %H:%M:%S"))
        await message.reply(text, parse_mode="Markdown" if markdown else None)

    @dp.message(Command("start", "help"))
    async def cmd_help(message: Message):
        await reply_command(message, (
            "🥊 **Привіт! Я ваш сімейний AI-тренер @SportNerdBot!**\n\n"
            "📌 **Швидкі команди:**\n"
            "/status — поточний стан виконання на сьогодні\n"
            "/rules — правила челенджу\n\n"
            "💡 **Як звітувати:**\n"
            "Надсилайте відео, кружечки або пишіть словами (*«Льоша здав відос»*, *«Саша закрив свої 26»*)."
        ))

    @dp.message(Command("rules"))
    async def cmd_rules(message: Message):
        await reply_command(message, challenge.rules_text(store), markdown=False)

    @dp.message(Command("status"))
    async def cmd_status(message: Message):
        s = challenge.status_snapshot(store, flow.now().date())
        lines = [f"📊 **СТАТУС НА СЬОГОДНІ ({s['date']}) — {s['exercise_name'].upper()}:**\n"]
        for p, meta in challenge.PARTICIPANTS.items():
            state = "✅ ВИКОНАНО" if s["done"][p] else f"❌ Ще потрібно `{s['targets'][p]}`"
            lives = f" | ❤️ {s['lives'][p]}" if s["lives"][p] is not None else ""
            lines.append(f"{meta['emoji']} **{meta['name']}:** {state}{lives}")
        lines.append("\n🎉 **Обоє закрили день!** 🥇" if all(s["done"].values()) else "\n⚡ Дотискайте норму, день ще триває!")
        await reply_command(message, "\n".join(lines))

    @dp.message()
    async def on_message(message: Message):
        if message.text and message.text.startswith("/"):
            return
        sender = message.from_user.full_name or message.from_user.first_name or "Користувач"
        flow.record_user_message(message.chat.id, sender, describe_message(message),
                                 message.date.astimezone(TIMEZONE))
        await flow.process(message.chat.id, trigger="push")

    return bot, dp, flow


async def main():
    setup_logging()
    bot, dp, flow = build_app()
    flow.init_storage()

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    for job, hour, minute in (("settle", 0, 5), ("morning", 8, 30), ("afternoon", 15, 0),
                              ("evening", 19, 0), ("night", 23, 30)):
        scheduler.add_job(flow.process, "cron", hour=hour, minute=minute,
                          args=[CHAT_ID], kwargs={"trigger": "timer", "job": job}, id=job)
    scheduler.start()
    logging.info("Scheduler started (00:05 settle, 08:30, 15:00, 19:00, 23:30 Kyiv).")

    try:
        await flow.process(CHAT_ID, trigger="startup")
    except Exception as e:
        logging.warning("Startup processing failed: %s", e)

    retry_delay = 5
    while True:
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            logging.info("SportNerdBot polling started")
            await dp.start_polling(bot, handle_signals=False)
            break
        except (KeyboardInterrupt, SystemExit):
            break
        except Exception as e:
            logging.warning("Polling error: %s. Retrying in %ds...", e, retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(int(retry_delay * 1.5), 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped.")
