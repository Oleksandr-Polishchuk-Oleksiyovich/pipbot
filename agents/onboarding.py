"""
Агент онбордингу (опитування) нових або недозаповнених учасників (status = 'uninitialized').
Використовує окремий промпт 'onboarding' із таблиці `prompts` у БД.
Кроки опитування обираються динамічно на базі вже відомих полів юзера і можуть пропускатися.
"""
import json
import logging
import re
from datetime import date
from typing import List, Optional

from agents.base import PromptAgent, render_messages
from core import challenge
from database.store import ChatMessage, DIFFICULTY_LIVES, Store, User

logger = logging.getLogger(__name__)

_DIFF_ALIASES = {
    "hardcore": "hardcore",
    "хардкор": "hardcore",
    "хард": "hardcore",
    "2": "hardcore",
    "normal": "normal",
    "норма": "normal",
    "норм": "normal",
    " звичайний": "normal",
    "5": "normal",
    "welcome": "welcome",
    "вітальний": "welcome",
    "вітальн": "welcome",
    "привітальний": "welcome",
    "лайт": "welcome",
    "10": "welcome",
}


def normalize_difficulty(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    val = raw.lower().strip()
    if val in DIFFICULTY_LIVES:
        return val
    for token, mode in _DIFF_ALIASES.items():
        if token in val:
            return mode
    return None


class OnboardingAgent(PromptAgent):
    prompt_key = "onboarding"

    def _tools(self, today: date, user_slug: str, actions: List[str]) -> list:
        store = self.store

        def save_onboarding_progress(
            display_name: Optional[str] = None,
            description: Optional[str] = None,
            max_pushups: Optional[int] = None,
            max_squats: Optional[int] = None,
            preferred_start_pushups: Optional[int] = None,
            difficulty: Optional[str] = None,
            emoji: Optional[str] = None,
        ) -> str:
            """
            Зберегти відомі дані про нового учасника під час опитування.
            Викликай щоразу, коли користувач назвав ім'я, опис про себе, максимум разів за підхід або бажаний режим.
            :param display_name: як називати учасника у чаті.
            :param description: короткий опис про себе або побажання щодо стилю звертання (опціонально).
            :param max_pushups: скільки максимум разів може віджатися за підхід.
            :param max_squats: скільки максимум разів може присісти за підхід.
            :param preferred_start_pushups: погоджена або обрана користувачем стартова норма віджимань.
            :param difficulty: обраний режим життів ('hardcore' — 2, 'normal' — 5, 'welcome' — 10).
            :param emoji: емодзі учасника (за замовчуванням '🏋️').
            """
            u = store.get_user(user_slug)
            if u is None:
                return f"Помилка: користувача {user_slug} не знайдено."
            notes = []
            if display_name and display_name.strip():
                u.display_name = display_name.strip()
                if u.display_name.lower() not in [a.lower() for a in u.aliases]:
                    u.aliases.append(u.display_name.lower())
                notes.append(f"ім'я={u.display_name}")
            if description is not None and description.strip():
                u.description = description.strip()
                notes.append("опис збережено")
            if emoji and emoji.strip():
                u.emoji = emoji.strip()
            if max_pushups is not None and int(max_pushups) > 0:
                u.max_pushups_reported = int(max_pushups)
                rec = challenge.recommended_start_reps(u.max_pushups_reported)
                notes.append(f"макс_віджимань={u.max_pushups_reported} (рекомендована 1/3 для старту = {rec})")
            if max_squats is not None and int(max_squats) > 0:
                u.max_squats_reported = int(max_squats)
                rec_sq = challenge.recommended_start_reps(u.max_squats_reported)
                notes.append(f"макс_присідань={u.max_squats_reported} (1/3 = {rec_sq})")
            norm_diff = normalize_difficulty(difficulty)
            if norm_diff:
                u.difficulty = norm_diff
                u.lives = DIFFICULTY_LIVES[norm_diff]
                u.max_lives = DIFFICULTY_LIVES[norm_diff]
                notes.append(f"режим={norm_diff} ({u.lives} життів)")
            store.upsert_user(u)
            if preferred_start_pushups is not None and int(preferred_start_pushups) > 0:
                challenge.set_starting_pushups(store, u.slug, int(preferred_start_pushups), today)
                notes.append(f"стартова_норма={int(preferred_start_pushups)}")
            actions.append(f"save_onboarding_progress({', '.join(notes)})")
            return "Збережено: " + (", ".join(notes) or "без змін")

        def complete_onboarding(
            starting_pushups: int,
            difficulty: str = "normal",
            display_name: Optional[str] = None,
            description: Optional[str] = None,
            emoji: Optional[str] = None,
        ) -> str:
            """
            Завершити опитування та активувати учасника в челенджі (status = 'active').
            Викликай, коли вже відома стартова норма (1/3 від максимуму або власне число юзера) та обраний режим життів.
            :param starting_pushups: стартова норма віджимань на сьогодні (ціле число >= 1).
            :param difficulty: режим життів: 'hardcore' (2 життя), 'normal' (5 життів) або 'welcome' (10 життів).
            :param display_name: фінальне ім'я для звертання (якщо змінювалось).
            :param description: короткий опис про себе (якщо користувач його надав).
            :param emoji: емодзі учасника.
            """
            u = store.get_user(user_slug)
            if u is None:
                return f"Помилка: користувача {user_slug} не знайдено."
            if display_name and display_name.strip():
                u.display_name = display_name.strip()
                if u.display_name.lower() not in [a.lower() for a in u.aliases]:
                    u.aliases.append(u.display_name.lower())
            if description is not None and description.strip():
                u.description = description.strip()
            if emoji and emoji.strip():
                u.emoji = emoji.strip()

            norm_diff = normalize_difficulty(difficulty) or "normal"
            lives_count = DIFFICULTY_LIVES[norm_diff]
            u.difficulty = norm_diff
            u.lives = lives_count
            u.max_lives = lives_count
            u.status = "active"
            u.joined_date = today.isoformat()
            store.upsert_user(u)

            challenge.set_starting_pushups(store, u.slug, max(1, int(starting_pushups)), today)
            today_target = challenge.day_targets(store, today).get(u.slug, int(starting_pushups))
            ex_name = challenge.EXERCISES[challenge.day_exercise(store, today)]["genitive"]
            actions.append(f"complete_onboarding(slug={u.slug}, start={starting_pushups}, mode={norm_diff}, lives={lives_count})")
            return (
                f"Учасника {u.display_name} активовано! Режим: {norm_diff} ({lives_count}/{lives_count} ❤️). "
                f"Норма на сьогодні ({today.isoformat()}): {today_target} {ex_name}."
            )

        return [save_onboarding_progress, complete_onboarding]

    def interview(self, today: date, user: User, pending: List[ChatMessage], history: List[ChatMessage]) -> tuple[str, List[str]]:
        actions: List[str] = []
        rec_pushups = (
            challenge.recommended_start_reps(user.max_pushups_reported)
            if user.max_pushups_reported
            else None
        )
        profile_state = {
            "slug": user.slug,
            "telegram_name": user.telegram_name,
            "display_name": user.display_name,
            "description": user.description or None,
            "max_pushups_reported": user.max_pushups_reported,
            "recommended_one_third_pushups": rec_pushups,
            "max_squats_reported": user.max_squats_reported,
            "difficulty": user.difficulty if user.lives is not None else None,
            "lives": user.lives,
            "max_lives": user.max_lives,
            "status": user.status,
            "today_exercise": challenge.EXERCISES[challenge.day_exercise(self.store, today)]["name"],
        }
        prompt = (
            f"ПОТОЧНИЙ ПРОФІЛЬ ЮЗЕРА В БД (що вже відомо / що ще порожнє):\n"
            f"{json.dumps(profile_state, ensure_ascii=False, indent=2)}\n\n"
            f"ІСТОРІЯ ЧАТУ ЗА СЬОГОДНІ:\n{render_messages(history)}\n\n"
            f"НОВІ ПОВІДОМЛЕННЯ ВІД КОРИСТУВАЧА ({user.display_name}):\n{render_messages(pending)}\n\n"
            "Проаналізуй, що користувач уже повідомив, збережи дані через інструменти та дай відповідь "
            "(або заверши онбординг викликом complete_onboarding, якщо стартова норма й режим уже відомі)."
        )
        reply = self.llm.text(
            self.system_prompt,
            prompt,
            tools=self._tools(today, user.slug, actions),
            temperature=0.6,
        )
        logger.info("Onboarding actions for %s: %s", user.slug, actions)
        return reply, actions

    @staticmethod
    def fallback(store: Store, user: User, pending: List[ChatMessage], today: date) -> str:
        """
        Детермінований фолбек онбордингу (коли Gemini недоступний або в офлайн-тестах).
        Дозволяє пройти опитування за кроками або в один крок, якщо всі дані вказані.
        """
        combined = " ".join(m.text for m in pending).strip()
        lower = combined.lower()

        # 1. Ім'я звертання (наприклад: «мене звати Макс», «називай мене Макс»)
        name_match = re.search(
            r"(?:мене\s+звати|називай\s+мене|клич\s+мене)\s+([А-ЯІЇЄҐа-яіїєґA-Za-z]{2,20})",
            combined,
            re.I,
        )
        if name_match:
            candidate = name_match.group(1).strip()
            if candidate.lower() not in ("можу", "роблю", "віджимаюсь", "присідаю", "хочу", "давай"):
                user.display_name = candidate
                if candidate.lower() not in [a.lower() for a in user.aliases]:
                    user.aliases.append(candidate.lower())

        # 2. Режим життів (Хардкор=2, Норма=5, Вітальний=10)
        detected_diff = None
        for word, mode in (
            ("хардкор", "hardcore"), ("hardcore", "hardcore"), ("2 життя", "hardcore"), ("2 життів", "hardcore"),
            ("вітальн", "welcome"), ("welcome", "welcome"), ("10 життів", "welcome"), ("10 життя", "welcome"),
            ("норма", "normal"), ("normal", "normal"), ("5 життів", "normal"),
        ):
            if word in lower:
                detected_diff = mode
                break
        if detected_diff:
            user.difficulty = detected_diff
            user.lives = DIFFICULTY_LIVES[detected_diff]
            user.max_lives = DIFFICULTY_LIVES[detected_diff]

        # 3. Числа у повідомленні (максимум або бажана стартова норма)
        numbers = [int(n) for n in re.findall(r"\b(\d{1,3})\b", combined) if int(n) not in (2, 5, 10) or "віджим" in lower or "норм" in lower or "почн" in lower or "можу" in lower]
        explicit_start = None
        start_match = re.search(r"(?:почн\w*\s+з|старт\w*|норм\w*)\s*(\d{1,3})", lower)
        if start_match:
            explicit_start = int(start_match.group(1))
        elif "третин" in lower and user.max_pushups_reported:
            explicit_start = challenge.recommended_start_reps(user.max_pushups_reported)
        elif numbers:
            if user.max_pushups_reported is None:
                user.max_pushups_reported = numbers[0]
                if "третин" in lower:
                    explicit_start = challenge.recommended_start_reps(user.max_pushups_reported)
            else:
                explicit_start = numbers[0]

        store.upsert_user(user)

        # Якщо відомі і стартова норма (або є макс + обрано режим і згода), і режим життів — активуємо!
        start_norm = explicit_start or (
            challenge.recommended_start_reps(user.max_pushups_reported)
            if (user.max_pushups_reported and detected_diff)
            else None
        )
        if start_norm is not None and user.lives is not None:
            user.status = "active"
            user.joined_date = today.isoformat()
            store.upsert_user(user)
            challenge.set_starting_pushups(store, user.slug, start_norm, today)
            target = challenge.day_targets(store, today).get(user.slug, start_norm)
            ex = challenge.EXERCISES[challenge.day_exercise(store, today)]["genitive"]
            return (
                f"🔥 **Вітаю в челенджі, {user.display_name}!**\n\n"
                f"🎯 Твоя стартова норма на сьогодні: **{target}** {ex}.\n"
                f"❤️ Режим: **{user.difficulty}** (**{user.lives}/{user.max_lives}** життів). Жодних виправдань — до бою! 👊"
            )

        # Якщо відомий максимум, але ще не підтверджено норму чи режим — пропонуємо 1/3 та режими 2/5/10
        if user.max_pushups_reported is not None:
            rec = challenge.recommended_start_reps(user.max_pushups_reported)
            return (
                f"🦾 **{user.display_name}**, крутий результат (**{user.max_pushups_reported}** за раз)!\n\n"
                f"🎯 Пропоную почати з **третини — `{rec}` віджимань** (або напиши будь-яке інше стартове число за бажанням).\n\n"
                "❤️ Обери свій режим виживання:\n"
                "• 🔥 **Хардкор** — **2 життя**\n"
                "• ⚡ **Норма** — **5 життів**\n"
                "• 🌱 **Вітальний** — **10 життів**"
            )

        # Перше привітання для нового юзера
        return (
            f"🏋️‍♂️ **Привіт, {user.display_name}! Я @SportNerdBot — твій новий тренер-мотиватор!**\n\n"
            "Щоб додати тебе в челендж, розкажи коротко:\n"
            "1. 👤 Як тебе називати (і за бажанням — короткий опис про себе)?\n"
            "2. 💪 Скільки максимум разів можеш віджатися чи присісти за підхід? (Я запропоную почати з **1/3**, або обереш своє число).\n"
            "3. ❤️ Який режим береш: **Хардкор (2 життя)**, **Норма (5 життів)** чи **Вітальний (10 життів)**?"
        )
