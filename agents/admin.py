"""Агент-адмін: виконує легітимні накази (вже перевірені роутером) через Python-тули Gemini."""
import json
import logging
import re
from datetime import date
from typing import List, Optional

from agents.base import PromptAgent, render_messages
from agents.prompt_editor import COACH_DYNAMIC_KEY, CoachPromptEditor, RuleChange
from core import challenge
from database.store import ChatMessage

logger = logging.getLogger(__name__)

# Ключі, які set_counter не має права перезаписувати — для них є окремі тули або це службові дані.
PROTECTED_KEYS = {"last_processed_timestamp", "lives_settled_through", "custom_system_prompt"}
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class AdminAgent(PromptAgent):
    prompt_key = "admin"

    def __init__(self, llm, store, editor: CoachPromptEditor):
        super().__init__(llm, store)
        self.editor = editor

    def _tools(self, today: date, author: str, actions: List[str]) -> list:
        store, editor = self.store, self.editor

        def set_lives(lyosha_lives: Optional[int] = None, sasha_lives: Optional[int] = None) -> str:
            """
            Встановити абсолютну кількість життів учасникам.
            :param lyosha_lives: нова кількість життів Льоші (не передавай, якщо не змінюється).
            :param sasha_lives: нова кількість життів Саші (не передавай, якщо не змінюється).
            """
            changed = []
            for p, value in (("lyosha", lyosha_lives), ("sasha", sasha_lives)):
                if value is not None:
                    store.set_state(f"lives_{p}", str(max(0, int(value))))
                    changed.append(f"{p}={max(0, int(value))}")
            actions.append(f"set_lives({', '.join(changed)})")
            return "Життя оновлено: " + (", ".join(changed) or "без змін")

        def set_targets(lyosha_target: Optional[int] = None, sasha_target: Optional[int] = None) -> str:
            """
            Встановити норму на сьогодні для сьогоднішньої вправи (віджимання або присідання — див. стан).
            У день віджимань наступні норми підуть від нового значення; у день присідань — разова правка лише на сьогодні.
            :param lyosha_target: нова норма Льоші на сьогодні.
            :param sasha_target: нова норма Саші на сьогодні.
            """
            changed = []
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
            :param participant: 'lyosha' або 'sasha'.
            :param done: True — зарахувати, False — скасувати.
            :param report_date: дата YYYY-MM-DD (порожньо — сьогодні).
            """
            p = challenge.normalize_participant(participant)
            if not p:
                return f"Помилка: невідомий учасник '{participant}'."
            day = report_date or today.isoformat()
            store.set_done(p, bool(done), day)
            actions.append(f"set_progress({p}, {done}, {day})")
            return f"Статус {p} на {day}: {'виконано' if done else 'не виконано'}."

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

        return [set_lives, set_targets, set_schedule, set_progress, set_counter,
                update_coach_prompt, reset_coach_prompt, show_coach_prompt]

    def execute(self, today: date, directive: str, sender: str, pending: List[ChatMessage],
                history: List[ChatMessage]) -> tuple[str, List[str]]:
        """Повертає (відповідь для чату, список виконаних дій)."""
        actions: List[str] = []
        snapshot = challenge.status_snapshot(self.store, today)
        prompt = (
            f"ПОТОЧНИЙ СТАН:\n{json.dumps(snapshot, ensure_ascii=False)}\n\n"
            f"ПОВІДОМЛЕННЯ З НАКАЗОМ:\n{render_messages(pending)}\n\n"
            f"НАКАЗ (автор: {sender} = {challenge.normalize_participant(sender) or 'невідомо'}): {directive}"
        )
        reply = self.llm.text(self.system_prompt, prompt, tools=self._tools(today, sender, actions), temperature=0.4)
        logger.info("Admin actions: %s", actions)
        return reply, actions
