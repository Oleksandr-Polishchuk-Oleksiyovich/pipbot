"""Агент-адмін: виконує легітимні накази (вже перевірені роутером) через Python-тули Gemini."""
import json
import logging
import re
from datetime import date
from typing import Callable, List, Optional

from agents.base import PromptAgent, render_messages
from agents.onboarding import normalize_difficulty
from agents.prompt_editor import COACH_DYNAMIC_KEY, CoachPromptEditor, RuleChange
from core import challenge
from database.store import ChatMessage, DIFFICULTY_LIVES

logger = logging.getLogger(__name__)

# Ключі, які set_counter не має права перезаписувати — для них є окремі тули або це службові дані.
PROTECTED_KEYS = {"last_processed_timestamp", "last_processed_id", "lives_settled_through", "custom_system_prompt"}
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class AdminAgent(PromptAgent):
    prompt_key = "admin"

    def __init__(self, llm, store, editor: CoachPromptEditor, chat_members_fetcher: Optional[Callable[[], dict]] = None):
        super().__init__(llm, store)
        self.editor = editor
        self.chat_members_fetcher = chat_members_fetcher

    def _tools(self, today: date, author: str, actions: List[str]) -> list:
        store, editor, fetcher = self.store, self.editor, self.chat_members_fetcher

        def set_lives(
            participant: Optional[str] = None,
            lives: Optional[int] = None,
            max_lives: Optional[int] = None,
            lyosha_lives: Optional[int] = None,
            sasha_lives: Optional[int] = None,
        ) -> str:
            """
            Встановити абсолютну кількість поточних (і за потреби максимальних) життів учаснику або учасникам.
            Можна передати (participant='<slug або імʼя>', lives=..., max_lives=...) або іменовані параметри.
            :param participant: ключ slug або ім'я учасника (наприклад 'sasha', 'lyosha' або інший учасник із БД).
            :param lives: нова поточна кількість життів для participant.
            :param max_lives: нова максимальна кількість життів для participant (опціонально).
            :param lyosha_lives: нова кількість життів Льоші (опціонально).
            :param sasha_lives: нова кількість життів Саші (опціонально).
            """
            changed = []
            if participant and lives is not None:
                slug = challenge.normalize_participant(participant, store) or participant
                user = challenge.set_user_lives(store, slug, int(lives), int(max_lives) if max_lives is not None else None)
                changed.append(f"{user.slug}={user.lives}/{user.max_lives}")
            for p, value in (("lyosha", lyosha_lives), ("sasha", sasha_lives)):
                if value is not None:
                    user = challenge.set_user_lives(store, p, int(value))
                    changed.append(f"{user.slug}={user.lives}/{user.max_lives}")
            actions.append(f"set_lives({', '.join(changed)})")
            return "Життя оновлено: " + (", ".join(changed) or "без змін")

        def set_targets(
            participant: Optional[str] = None,
            target: Optional[int] = None,
            all_target: Optional[int] = None,
            lyosha_target: Optional[int] = None,
            sasha_target: Optional[int] = None,
        ) -> str:
            """
            Встановити норму на сьогодні для сьогоднішньої вправи (віджимання або присідання — див. стан).
            У день віджимань наступні норми підуть від нового значення; у день присідань — разова правка лише на сьогодні.
            :param participant: ключ slug або ім'я учасника (або 'all' для всіх активних учасників).
            :param target: нова норма для вказаного participant на сьогодні.
            :param all_target: нова норма однакова для всіх активних учасників на сьогодні (опціонально).
            :param lyosha_target: нова норма Льоші на сьогодні (опціонально).
            :param sasha_target: нова норма Саші на сьогодні (опціонально).
            """
            changed = []
            if all_target is not None or (participant and participant.lower() in ("all", "всі", "обом", "усім") and target is not None):
                val = int(all_target if all_target is not None else target)
                for u in store.list_users(status="active"):
                    challenge.set_target(store, u.slug, val, today)
                    changed.append(f"{u.slug}={val}")
            elif participant and target is not None:
                slug = challenge.normalize_participant(participant, store) or participant
                challenge.set_target(store, slug, int(target), today)
                changed.append(f"{slug}={int(target)}")
            for p, value in (("lyosha", lyosha_target), ("sasha", sasha_target)):
                if value is not None:
                    challenge.set_target(store, p, int(value), today)
                    changed.append(f"{p}={int(value)}")
            actions.append(f"set_targets({', '.join(changed)})")
            return "Норми оновлено: " + (", ".join(changed) or "без змін")

        def set_schedule(mode: str, squat_factor: float = 1.5) -> str:
            """
            Перемкнути розклад вправ.
            :param mode: 'alternate' — чергування віджимання/присідання з сьогоднішнього дня (сьогодні — віджимання
                         з поточними нормами; присідання = squat_factor × учорашні віджимання, вгору до цілого);
                         'ladder' — щодня віджимання +1.
            :param squat_factor: коефіцієнт присідань для режиму 'alternate'.
            """
            if mode == "alternate":
                challenge.enable_alternating(store, today, challenge.day_targets(store, today), squat_factor)
            elif mode == "ladder":
                challenge.disable_alternating(store, today)
            else:
                return f"Помилка: невідомий режим '{mode}'."
            actions.append(f"set_schedule({mode}, {squat_factor})")
            return (f"Розклад: {mode}. Сьогодні: {challenge.day_exercise(store, today)} "
                    f"{challenge.day_targets(store, today)}.")

        def set_progress(participant: str, done: bool, report_date: str = "") -> str:
            """
            Вручну зарахувати або скасувати виконання норми.
            :param participant: ключ slug / ім'я учасника або 'all' (для всіх активних учасників).
            :param done: True — зарахувати, False — скасувати.
            :param report_date: дата YYYY-MM-DD (порожньо — сьогодні).
            """
            day = report_date or today.isoformat()
            if participant.lower().strip() in ("all", "всі", "усі", "обом"):
                slugs = [u.slug for u in store.list_users(status="active")]
                for s in slugs:
                    store.set_done(s, bool(done), day)
                actions.append(f"set_progress(all={slugs}, {done}, {day})")
                return f"Статус усіх учасників ({', '.join(slugs)}) на {day}: {'виконано' if done else 'не виконано'}."
            p = challenge.normalize_participant(participant, store)
            if not p:
                return f"Помилка: невідомий учасник '{participant}'."
            store.set_done(p, bool(done), day)
            actions.append(f"set_progress({p}, {done}, {day})")
            return f"Статус {p} на {day}: {'виконано' if done else 'не виконано'}."

        def update_user_profile(
            participant: str,
            display_name: Optional[str] = None,
            description: Optional[str] = None,
            difficulty: Optional[str] = None,
            starting_pushups: Optional[int] = None,
            emoji: Optional[str] = None,
            status: Optional[str] = None,
        ) -> str:
            """
            Оновити профіль учасника або створити/активувати учасника в таблиці users.
            :param participant: slug або ім'я учасника.
            :param display_name: як називати учасника.
            :param description: короткий опис про учасника та бажаний тон спілкування.
            :param difficulty: режим життів ('hardcore' — 2, 'normal' — 5, 'welcome' — 10).
            :param starting_pushups: стартова норма віджимань на сьогодні (якщо встановлюється).
            :param emoji: емодзі учасника.
            :param status: статус ('active', 'uninitialized', 'inactive').
            """
            u = store.find_user(participant) or store.resolve_or_create_user(participant, today.isoformat())
            if display_name and display_name.strip():
                u.display_name = display_name.strip()
                if u.display_name.lower() not in [a.lower() for a in u.aliases]:
                    u.aliases.append(u.display_name.lower())
            if description is not None:
                u.description = description.strip()
            if emoji and emoji.strip():
                u.emoji = emoji.strip()
            norm_diff = normalize_difficulty(difficulty)
            if norm_diff:
                u.difficulty = norm_diff
                u.lives = DIFFICULTY_LIVES[norm_diff]
                u.max_lives = DIFFICULTY_LIVES[norm_diff]
            if status in ("active", "uninitialized", "inactive"):
                u.status = status
            store.upsert_user(u)
            if starting_pushups is not None:
                challenge.set_starting_pushups(store, u.slug, int(starting_pushups), today)
            actions.append(f"update_user_profile({u.slug}, status={u.status})")
            return f"Профіль {u.slug} ({u.display_name}) оновлено: status={u.status}, lives={u.lives}/{u.max_lives}."

        def get_chat_members_info() -> str:
            """Отримати список людей із Telegram-чату та всіх користувачів, зареєстрованих у БД."""
            db_users = [
                {
                    "slug": u.slug,
                    "display_name": u.display_name,
                    "telegram_name": u.telegram_name,
                    "username": u.username,
                    "status": u.status,
                    "difficulty": u.difficulty,
                    "lives": u.lives,
                    "max_lives": u.max_lives,
                }
                for u in store.list_users()
            ]
            tg_info = fetcher() if fetcher else {}
            actions.append("get_chat_members_info()")
            return json.dumps({"db_users": db_users, "telegram_chat": tg_info}, ensure_ascii=False)

        def set_counter(key: str, value: str, description: str) -> str:
            """
            Створити або змінити довільний лічильник/значення стану бота.
            :param key: ім'я ключа у snake_case (наприклад 'bonus_points_lyosha').
            :param value: нове значення (рядком).
            :param description: що означає цей лічильник.
            """
            if not _KEY_RE.match(key) or key in PROTECTED_KEYS:
                return f"Помилка: ключ '{key}' недопустимий."
            store.set_state(key, value)
            actions.append(f"set_counter({key}={value})")
            return f"Лічильник {key} = {value} ({description})."

        def update_coach_prompt(rule: str, original_quote: str, valid_until: str = "") -> str:
            """
            Змінити поведінку, стиль, звертання, титули чи правила спілкування тренера.
            :param rule: одне атомарне правило — повне, однозначне, від третьої особи («Льошу називати …»).
            :param original_quote: ДОСЛІВНА цитата наказу з чату (з усіма назвами, титулами й числами як є).
            :param valid_until: '' — постійне правило; або дата YYYY-MM-DD, до якої правило діє включно.
            """
            change = RuleChange(rule=rule, quote=original_quote, until=valid_until,
                                author=author, issued=today.isoformat())
            result = editor.apply(change)
            actions.append(f"update_coach_prompt(strategy={result.strategy}, v{result.version})")
            if not result.changed:
                return f"Правило вже діє, змін не потрібно ({result.reason})."
            return f"Правила тренера оновлено (стратегія: {result.strategy}, версія {result.version})."

        def reset_coach_prompt() -> str:
            """Скинути всі динамічні правила тренера до порожніх (лишається тільки базова роль)."""
            version = editor.reset(author)
            actions.append(f"reset_coach_prompt(v{version})")
            return f"Динамічні правила скинуто (версія {version})."

        def show_coach_prompt() -> str:
            """Показати чинні динамічні правила тренера."""
            return store.get_prompt(COACH_DYNAMIC_KEY) or "(динамічних правил немає)"

        return [
            set_lives, set_targets, set_schedule, set_progress, update_user_profile,
            get_chat_members_info, set_counter, update_coach_prompt, reset_coach_prompt, show_coach_prompt,
        ]

    def execute(self, today: date, directive: str, sender: str, pending: List[ChatMessage],
                history: List[ChatMessage]) -> tuple[str, List[str]]:
        """Повертає (відповідь для чату, список виконаних дій)."""
        actions: List[str] = []
        snapshot = challenge.status_snapshot(self.store, today)
        sender_slug = challenge.normalize_participant(sender, self.store) or "невідомо"
        prompt = (
            f"ПОТОЧНИЙ СТАН:\n{json.dumps(snapshot, ensure_ascii=False)}\n\n"
            f"ПОВІДОМЛЕННЯ З НАКАЗОМ:\n{render_messages(pending)}\n\n"
            f"НАКАЗ (автор: {sender} = {sender_slug}): {directive}"
        )
        reply = self.llm.text(self.system_prompt, prompt, tools=self._tools(today, sender, actions), temperature=0.4)
        logger.info("Admin actions: %s", actions)
        return reply, actions
