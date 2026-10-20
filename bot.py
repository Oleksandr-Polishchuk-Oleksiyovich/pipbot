"""Telegram-адаптер (aiogram) + планувальник. Уся логіка — в app/flow.py та agents/."""
import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional

try:
    from aiogram import Bot, Dispatcher
    from aiogram.filters import Command
    from aiogram.types import FSInputFile, Message
except ImportError:
    Bot = Dispatcher = Command = FSInputFile = Message = None  # type: ignore
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

    async def send_text(self, chat_id: int, text: str, reply_to_message_id: Optional[int] = None) -> None:
        """Спершу Markdown; якщо Telegram не розпарсив розмітку — відправляємо чистим текстом."""
        if not text:
            return
        clean = text.replace("***", "— — —")
        kwargs = {}
        if reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id
        try:
            await self.bot.send_message(chat_id, clean, parse_mode="Markdown", **kwargs)
        except Exception as e:
            logging.warning("Markdown send failed (%s), retrying as plain text", e)
            await self.bot.send_message(chat_id, clean, parse_mode=None, **kwargs)

    async def send_video(self, chat_id: int, path: str) -> None:
        await self.bot.send_video(chat_id, FSInputFile(path))

    async def fetch_chat_members(self, chat_id: int) -> dict:
        """Дістає доступну з Telegram API інформацію про учасників чату (кількість та список адмінів/творця)."""
        count = await self.bot.get_chat_member_count(chat_id)
        admins = await self.bot.get_chat_administrators(chat_id)
        members = []
        for m in admins:
            u = m.user
            if not u.is_bot:
                members.append({
                    "telegram_id": u.id,
                    "full_name": u.full_name or u.first_name or "",
                    "username": u.username or "",
                    "role": m.status,
                })
        return {"member_count": count, "visible_members": members}


def describe_message(message: Message) -> str:
    """Текстове представлення повідомлення для журналу (медіа, підписи, контекст reply)."""
    reply_context = ""
    if getattr(message, "reply_to_message", None):
        orig = message.reply_to_message
        orig_user = getattr(orig, "from_user", None)
        orig_sender = (orig_user.full_name or orig_user.first_name) if orig_user else "Користувач"
        orig_text = getattr(orig, "text", None) or getattr(orig, "caption", None) or ""
        if not orig_text:
            if getattr(orig, "animation", None):
                orig_text = "[Анімація/GIF]"
            elif getattr(orig, "video", None) or getattr(orig, "video_note", None):
                orig_text = "[Відеозвіт]"
            elif getattr(orig, "photo", None):
                orig_text = "[Фотозвіт]"
            else:
                orig_text = "[Повідомлення]"
        reply_context = f"[У відповідь на {orig_sender}: «{orig_text}»] "

    caption = getattr(message, "caption", "") or ""
    if getattr(message, "animation", None):
        content = f"[Надіслано GIF/анімацію — не є відеозвітом]. {caption}".strip()
    elif getattr(message, "video", None) or getattr(message, "video_note", None):
        content = f"[Надіслано відеозвіт виконання норми]. {caption}".strip()
    elif getattr(message, "photo", None):
        content = f"[Надіслано фотозвіт]. {caption}".strip()
    elif getattr(message, "text", None):
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
        user = message.from_user
        name = (user.full_name or user.first_name) if user else "Користувач"
        flow.record_user_message(
            message.chat.id,
            name,
            message.text or "",
            telegram_id=user.id if user else None,
            username=user.username if user else None,
        )
        store.save_message(message.chat.id, "Спорт Зануда", "coach", text,
                           flow.now().strftime("%Y-%m-%d %H:%M:%S"))
        store.set_state("last_processed_timestamp", flow.now().strftime("%Y-%m-%d %H:%M:%S"))
        store.set_state("last_processed_id", str(store.get_last_message_id(message.chat.id)))
        await message.reply(text, parse_mode="Markdown" if markdown else None)

    @dp.message(Command("start", "help"))
    async def cmd_help(message: Message):
        await reply_command(message, (
            "🥊 **Привіт! Я ваш спортивний AI-тренер @SportNerdBot!**\n\n"
            "📌 **Швидкі команди:**\n"
            "/status — поточний стан виконання на сьогодні\n"
            "/rules — правила челенджу\n\n"
            "💡 **Як звітувати:**\n"
            "Надсилайте відео, кружечки або пишіть словами (*«здав відос»*, *«закрив свої 26»*).\n"
            "Нові учасники при першому повідомленні проходять коротке опитування і одразу вступають у челендж!"
        ))

    @dp.message(Command("rules"))
    async def cmd_rules(message: Message):
        await reply_command(message, challenge.rules_text(store), markdown=False)

    @dp.message(Command("status"))
    async def cmd_status(message: Message):
        s = challenge.status_snapshot(store, flow.now().date())
        lines = [f"📊 **СТАТУС НА СЬОГОДНІ ({s['date']}) — {s['exercise_name'].upper()}:**\n"]
        for p, meta in s["participants"].items():
            state = "✅ ВИКОНАНО" if meta["done"] else f"❌ Ще потрібно `{meta['target']}`"
            if meta["lives"] is not None:
                lives = f" | ❤️ {meta['lives']}/{meta['max_lives']}" if meta.get("max_lives") else f" | ❤️ {meta['lives']}"
            else:
                lives = ""
            lines.append(f"{meta['emoji']} **{meta['name']}:** {state}{lives}")
        if s.get("uninitialized"):
            names = ", ".join(u["display_name"] for u in s["uninitialized"])
            lines.append(f"\n🆕 *Очікують на завершення реєстрації:* {names}")
        lines.append("\n🎉 **Усі закрили день!** 🥇" if s["done"] and all(s["done"].values()) else "\n⚡ Дотискайте норму, день ще триває!")
        await reply_command(message, "\n".join(lines))

    @dp.message(Command("approve"))
    async def cmd_approve(message: Message):
        user = message.from_user
        name = (user.full_name or user.first_name) if user else "Користувач"
        u = flow.record_user_message(
            message.chat.id,
            name,
            message.text or "",
            telegram_id=user.id if user else None,
            username=user.username if user else None,
        )
        parts = (message.text or "").split()
        if len(parts) < 2:
            await reply_command(message, "⚠️ Використання: `/approve <request_id>`")
            return
        req_id = parts[1].strip()
        _, reply = await flow.handle_approve(message.chat.id, req_id, u, reply_to_message_id=message.message_id)

    @dp.message(Command("rollback"))
    async def cmd_rollback(message: Message):
        user = message.from_user
        name = (user.full_name or user.first_name) if user else "Користувач"
        u = flow.record_user_message(
            message.chat.id,
            name,
            message.text or "",
            telegram_id=user.id if user else None,
            username=user.username if user else None,
        )
        _, reply = await flow.handle_rollback(message.chat.id, u, reply_to_message_id=message.message_id)

    @dp.message()
    async def on_message(message: Message):
        if message.text and message.text.startswith("/"):
            return
        msg_dt = message.date.astimezone(TIMEZONE)
        # Якщо в чат додали нових людей — реєструємо кожного не-бота як 'uninitialized' і запускаємо онбординг
        if message.new_chat_members:
            added_any = False
            for member in message.new_chat_members:
                if member.is_bot:
                    continue
                m_name = member.full_name or member.first_name or f"User {member.id}"
                flow.record_user_message(
                    message.chat.id,
                    m_name,
                    f"[Новий учасник {m_name} приєднався до чату]",
                    msg_dt,
                    telegram_id=member.id,
                    username=member.username,
                )
                added_any = True
            if added_any:
                await flow.process(message.chat.id, trigger="push")
            return

        user = message.from_user
        sender = (user.full_name or user.first_name) if user else "Користувач"

        is_anim = bool(getattr(message, "animation", None))
        vid = getattr(message, "video", None)
        vnote = getattr(message, "video_note", None)
        is_video = bool(vid or vnote)

        if is_anim or is_video:
            media_obj = getattr(message, "animation", None) or vid or vnote
            file_id = getattr(media_obj, "file_id", None)
            file_unique_id = getattr(media_obj, "file_unique_id", None)
            duration = getattr(media_obj, "duration", None)
            file_size = getattr(media_obj, "file_size", None)

            user_record = flow.record_user_message(
                message.chat.id,
                sender,
                describe_message(message),
                msg_dt,
                telegram_id=user.id if user else None,
                username=user.username if user else None,
            )
            await flow.handle_video(
                message.chat.id,
                user_record,
                message.message_id,
                is_animation=is_anim,
                file_id=file_id,
                file_unique_id=file_unique_id,
                duration=float(duration) if duration is not None else None,
                file_size=file_size,
            )
            return

        flow.record_user_message(
            message.chat.id,
            sender,
            describe_message(message),
            msg_dt,
            telegram_id=user.id if user else None,
            username=user.username if user else None,
        )
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
    scheduler.add_job(flow.check_expired_offload_requests, "interval", minutes=1,
                      args=[CHAT_ID], id="check_offload_expiry")
    scheduler.start()
    logging.info("Scheduler started (00:05 settle, 08:30, 15:00, 19:00, 23:30 Kyiv).")

    try:
        await flow.drain_pending_replies()
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
