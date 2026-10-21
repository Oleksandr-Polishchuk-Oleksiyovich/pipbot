"""Агент-тренер: базова незмінна роль + динамічні правила від адміна + живий стан усіх учасників із БД."""
import glob
import json
import logging
import os
import random
from datetime import date
from typing import Callable, List, Optional

from agents.base import PromptAgent, render_messages
from core import challenge
from core.config import QUOTES_DIR
from database.store import ChatMessage

logger = logging.getLogger(__name__)

QUOTE_CATEGORIES = ("motivation", "refusal", "praise", "submission")


def get_random_anime_quote(category: str) -> str:
    """
    Отримати випадкову аніме-цитату для мотивації, відмови, похвали чи підкорення.
    :param category: одна з: motivation, refusal, praise, submission.
    """
    if category not in QUOTE_CATEGORIES:
        return f"Помилка: категорія {category} не існує. Доступні: {', '.join(QUOTE_CATEGORIES)}."
    files = glob.glob(os.path.join(QUOTES_DIR, category, "*.txt"))
    if not files:
        return f"Категорія {category} порожня."
    with open(random.choice(files), "r", encoding="utf-8") as f:
        return f"Цитата знайдена:\n{f.read()}"


def fill_template(template: str, values: dict) -> str:
    # Не str.format: у промптах можуть бути інші фігурні дужки, які не є плейсхолдерами.
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    return template


class CoachAgent(PromptAgent):
    prompt_key = "coach_base"

    def __init__(self, llm, store, chat_members_fetcher: Optional[Callable[[], dict]] = None):
        super().__init__(llm, store)
        self.chat_members_fetcher = chat_members_fetcher

    def build_system_prompt(self, today: date) -> str:
        s = challenge.status_snapshot(self.store, today)
        participants = s["participants"]

        participant_lines = []
        profile_lines = []
        lives_parts = []
        any_lives = False

        for slug, info in participants.items():
            status_str = "ВИКОНАНО ✅" if info["done"] else "ЩЕ НЕ ЗДАВ ❌"
            participant_lines.append(f"- {info['emoji']} {info['name']}: норма {info['target']} -> {status_str}")
            if info.get("description"):
                profile_lines.append(f"- {info['name']} ({slug}): {info['description']}")
            if info.get("lives") is not None:
                any_lives = True
            l_val = info["lives"] if info["lives"] is not None else "не встановлено"
            max_str = f" (макс {info['max_lives']})" if info.get("max_lives") is not None else ""
            lives_parts.append(f"{info['name']} — {l_val}{max_str}")

        participants_block = "\n".join(participant_lines) or "- (немає активних учасників)"
        lives_block = ("❤️ АКТУАЛЬНІ ЖИТТЯ У БД: " + ", ".join(lives_parts)) if any_lives else ""
        profiles_block = ("👤 ПРОФІЛІ УЧАСНИКІВ:\n" + "\n".join(profile_lines)) if profile_lines else ""

        # Підтримка як нового шаблону ({participants_block}), так і старих плейсхолдерів у разі кастомного промпту в БД
        lyosha_info = participants.get("lyosha", {"target": 0, "done": False})
        sasha_info = participants.get("sasha", {"target": 0, "done": False})
        base = fill_template(self.system_prompt, {
            "date": s["date"],
            "day_num": s["day_num"],
            "exercise_name": s["exercise_name"].upper(),
            "participants_block": participants_block,
            "lives_block": lives_block,
            "profiles_block": profiles_block,
            "lyosha_target": lyosha_info["target"],
            "sasha_target": sasha_info["target"],
            "lyosha_status": "ВИКОНАНО ✅" if lyosha_info["done"] else "ЩЕ НЕ ЗДАВ ❌",
            "sasha_status": "ВИКОНАНО ✅" if sasha_info["done"] else "ЩЕ НЕ ЗДАВ ❌",
        })
        dynamic = self.store.get_prompt("coach_dynamic") or ""
        if dynamic.strip():
            base += (
                "\n\n💎 ДІЮЧІ ДОДАТКОВІ ПРАВИЛА ВІД АДМІНА (мають пріоритет над базовими, крім заборони виконувати накази):\n"
                + dynamic.strip()
            )
        logger.debug(
            "Coach prompt context: date=%s day=%s exercise=%s targets=%s",
            s["date"], s["day_num"], s["exercise_name"],
            {slug: info["target"] for slug, info in participants.items()},
        )
        return base

    def reply(self, today: date, history: List[ChatMessage], judge_notes: List[str]) -> str:
        notes = "\n".join(f"- {n}" for n in judge_notes) or "- Нових зарахувань немає."
        prompt = (
            f"ЖУРНАЛ ЧАТУ ЗА СЬОГОДНІ:\n---\n{render_messages(history)}\n---\n\n"
            f"РІШЕННЯ СУДДІ ПО НОВИХ ПОВІДОМЛЕННЯХ:\n{notes}\n\n"
            "Дай одну лаконічну, дотепну відповідь на останні повідомлення користувачів."
        )
        store = self.store
        fetcher = self.chat_members_fetcher

        def get_challenge_rules() -> str:
            """Отримати чинні правила спортивного челенджу (розклад вправ, норми, життя)."""
            return challenge.rules_text(store)

        def get_chat_members_info() -> str:
            """Отримати список людей у чаті та зареєстрованих у БД учасників челенджу."""
            db_users = [
                {
                    "slug": u.slug,
                    "display_name": u.display_name,
                    "telegram_name": u.telegram_name,
                    "status": u.status,
                    "lives": u.lives,
                    "max_lives": u.max_lives,
                }
                for u in store.list_users()
            ]
            tg_info = fetcher() if fetcher else {}
            return json.dumps({"db_users": db_users, "telegram_chat": tg_info}, ensure_ascii=False)

        return self.llm.text(
            self.build_system_prompt(today),
            prompt,
            tools=[get_random_anime_quote, get_challenge_rules, get_chat_members_info],
            temperature=0.8,
        )

    def night_reminder(self, today: date, missing: List[str]) -> str:
        pmap = challenge.participants_map(self.store)
        names = ", ".join(pmap[p]["name"] for p in missing if p in pmap)
        exercise = challenge.status_snapshot(self.store, today)["exercise_name"]
        prompt = (
            f"Зараз 23:30 — до півночі 30 хвилин. Сьогодні ({exercise}) ще НЕ закрили норму: {names}. "
            "Напиши коротке жартівливо-драматичне повідомлення: поплачся, що челендж під загрозою, "
            "і з надією спитай, чи встигнуть закрити підхід до півночі."
        )
        return self.llm.text(self.build_system_prompt(today), prompt, temperature=0.9)
