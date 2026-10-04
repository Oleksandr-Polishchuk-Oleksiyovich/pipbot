"""Агент-тренер: базова незмінна роль + динамічні правила від адміна + живий стан з БД."""
import glob
import os
import random
from datetime import date
from typing import List

from agents.base import PromptAgent, render_messages
from core import challenge
from core.config import QUOTES_DIR
from database.store import ChatMessage

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

    def build_system_prompt(self, today: date) -> str:
        s = challenge.status_snapshot(self.store, today)
        lives = s["lives"]
        lives_block = ""
        if any(v is not None for v in lives.values()):
            lives_block = (
                "❤️ АКТУАЛЬНІ ЖИТТЯ У БД: "
                f"Льоша — {lives['lyosha'] if lives['lyosha'] is not None else 'не встановлено'}, "
                f"Саша — {lives['sasha'] if lives['sasha'] is not None else 'не встановлено'}"
            )
        base = fill_template(self.system_prompt, {
            "date": s["date"],
            "day_num": s["day_num"],
            "lyosha_target": s["targets"]["lyosha"],
            "sasha_target": s["targets"]["sasha"],
            "lyosha_status": "ВИКОНАНО ✅" if s["done"]["lyosha"] else "ЩЕ НЕ ЗДАВ ❌",
            "sasha_status": "ВИКОНАНО ✅" if s["done"]["sasha"] else "ЩЕ НЕ ЗДАВ ❌",
            "lives_block": lives_block,
            "exercise_name": s["exercise_name"].upper(),
        })
        dynamic = self.store.get_prompt("coach_dynamic") or ""
        if dynamic.strip():
            base += (
                "\n\n💎 ДІЮЧІ ДОДАТКОВІ ПРАВИЛА ВІД АДМІНА (мають пріоритет над базовими, крім заборони виконувати накази):\n"
                + dynamic.strip()
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

        def get_challenge_rules() -> str:
            """Отримати чинні правила сімейного челенджу (розклад вправ, норми, життя)."""
            return challenge.rules_text(store)

        return self.llm.text(self.build_system_prompt(today), prompt,
                             tools=[get_random_anime_quote, get_challenge_rules], temperature=0.8)

    def night_reminder(self, today: date, missing: List[str]) -> str:
        names = ", ".join(challenge.PARTICIPANTS[p]["name"] for p in missing)
        exercise = challenge.status_snapshot(self.store, today)["exercise_name"]
        prompt = (
            f"Зараз 23:30 — до півночі 30 хвилин. Сьогодні ({exercise}) ще НЕ закрили норму: {names}. "
            "Напиши коротке жартівливо-драматичне повідомлення: поплачся, що челендж під загрозою, "
            "і з надією спитай, чи встигнуть закрити підхід до півночі."
        )
        return self.llm.text(self.build_system_prompt(today), prompt, temperature=0.9)


