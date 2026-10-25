"""Спільні фікстури для тестів флоу: тимчасова БД, фейковий Telegram, фіксований годинник."""
from datetime import datetime

import pytest

from app.flow import ChatFlow
from core.config import TIMEZONE
from database.store import Store

CHAT = -100500
NOW = TIMEZONE.localize(datetime(2026, 10, 4, 11, 15, 0))  # день 35, норми: Льоша 37, Саша 47 (без зсувів)
SON = "Алексей"
DAD = "Oleksandr Polishchuk"


class FakeSender:
    def __init__(self):
        self.texts = []
        self.videos = []
        self.replies = []

    async def send_text(self, chat_id, text, reply_to_message_id=None, **kwargs):
        self.texts.append(text)
        if reply_to_message_id is not None:
            self.replies.append((reply_to_message_id, text))

    async def send_video(self, chat_id, path):
        self.videos.append(path)


class Clock:
    def __init__(self, now):
        self.value = now

    def __call__(self):
        return self.value

    def at(self, hour, minute=0, second=0):
        self.value = self.value.replace(hour=hour, minute=minute, second=second)
        return self.value


def make_flow(tmp_path, llm, trust_mode: bool = False):
    clock = Clock(NOW)
    sender = FakeSender()
    flow = ChatFlow(
        Store(str(tmp_path / "flow.db")),
        llm,
        sender,
        now=clock,
        obey_media_dir=str(tmp_path / "none"),
        trust_mode=trust_mode,
    )
    flow.init_storage()
    return flow, sender, clock


async def say(flow, clock, user, text, minute_step=1):
    """Користувач пише повідомлення → бот обробляє його як push."""
    clock.value = clock.value.replace(minute=(clock.value.minute + minute_step) % 60)
    flow.record_user_message(CHAT, user, text)
    await flow.process(CHAT, trigger="push")
