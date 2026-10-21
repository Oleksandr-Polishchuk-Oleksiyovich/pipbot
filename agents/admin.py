"""Агент-адмін: виконує легітимні накази (вже перевірені роутером) через Python-тули Gemini."""
import functools
import json
import logging
import re
from datetime import date, timedelta
from typing import Callable, List, Optional, Union

from agents.base import PromptAgent, render_messages
from agents.onboarding import normalize_difficulty
from agents.prompt_editor import COACH_DYNAMIC_KEY, CoachPromptEditor, RuleChange
from core import challenge
from core.bot_texts import BotTexts
from database.store import ChatMessage, DIFFICULTY_LIVES

logger = logging.getLogger(__name__)

# Ключі, які set_counter не має права перезаписувати — для них є окремі тули або це службові дані.
# Стан розкладу/норм/життів змінюється лише профільними тулами (set_schedule, set_targets, set_lives).
SCHEDULE_KEY_PREFIXES = ("schedule_", "alt_", "override_", "offset_", "lives_", "text:")
PROTECTED_KEYS = {"last_processed_timestamp", "last_processed_id", "lives_settled_through", "custom_system_prompt"}
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


def _logged_tool(fn):
    """Обгортає admin-тул логуванням кожного виклику (ім'я, args/kwargs) і результату."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        logger.info("Admin tool call: %s(args=%r, kwargs=%r)", fn.__name__, args, kwargs)
        try:
            result = fn(*args, **kwargs)
        except Exception:
            logger.exception("Admin tool call raised an exception: %s", fn.__name__)
            raise
        logger.info("Admin tool result: %s -> %r", fn.__name__, result)
        return result
    return wrapper


class AdminAgent(PromptAgent):
    prompt_key = "admin"

    def __init__(
        self,
        llm,
        store,
        editor: CoachPromptEditor,
        chat_members_fetcher: Optional[Callable[[], dict]] = None,
        offload_client: Optional[object] = None,
        box_available: Optional[Union[bool, Callable[[], bool]]] = None,
        bot_texts: Optional[BotTexts] = None,
    ):
        super().__init__(llm, store)
        self.editor = editor
        self.chat_members_fetcher = chat_members_fetcher
        self.offload_client = offload_client
        self._box_available = box_available
        # Той самий кешований сервіс, що й у ChatFlow — щоб правка діяла одразу, без рестарту.
        self.bot_texts = bot_texts or BotTexts(store)

    def is_box_available(self) -> bool:
        if self._box_available is not None:
            return bool(self._box_available() if callable(self._box_available) else self._box_available)
        if self.offload_client is not None:
            return bool(getattr(self.offload_client, "webhook_url", None))
        return False

    def _tools(self, today: date, author: str, actions: List[str]) -> list:
        store, editor, fetcher, bot_texts = self.store, self.editor, self.chat_members_fetcher, self.bot_texts

        def _today_tomorrow_summary() -> str:
            tomorrow = today + timedelta(days=1)
            t_ex, tm_ex = challenge.day_exercise(store, today), challenge.day_exercise(store, tomorrow)
            t_targets, tm_targets = challenge.day_targets(store, today), challenge.day_targets(store, tomorrow)
            return (
                f"Сьогодні ({today.isoformat()}, {challenge.EXERCISES[t_ex]['name']}): {t_targets}. "
                f"Завтра ({tomorrow.isoformat()}, {challenge.EXERCISES[tm_ex]['name']}): {tm_targets}."
            )

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

        def set_schedule(mode: str, squat_factor: float = 1.5, today_exercise: Optional[str] = None) -> str:
            """
            Перемкнути розклад вправ.
            :param mode: 'alternate' — чергування віджимання/присідання з сьогоднішнього дня (присідання =
                         squat_factor × пушапс-еквівалент дня, вгору до цілого); 'ladder' — щодня віджимання +1.
            :param squat_factor: коефіцієнт присідань для режиму 'alternate'.
            :param today_exercise: яка вправа СЬОГОДНІ в режимі 'alternate' — 'pushups' або 'squats'.
                         ОБОВ'ЯЗКОВО передавай явно, якщо адмін прямо називає сьогоднішню вправу
                         («сьогодні присідання», «зробимо сьогодні качання ніг», «з сьогодні навпаки —
                         спочатку присідання» тощо). Якщо не передано і розклад УЖЕ 'alternate' — поточна
                         вправа сьогодні НЕ змінюється (фаза зберігається, щоб не збити лічильник прогресії).
                         Якщо не передано і розклад ще 'ladder' — за замовчуванням сьогодні 'pushups'.
                         Для прохання «поміняй сьогодні вправу на протилежну» / «сьогодні навпаки» БЕЗ
                         називання конкретної вправи використовуй swap_today_exercise() замість цього.
            """
            before_mode = store.get_state("schedule_mode", "ladder")
            before_exercise = challenge.day_exercise(store, today)
            before_targets = challenge.day_targets(store, today)

            if mode == "alternate":
                if today_exercise is not None and today_exercise not in ("pushups", "squats"):
                    return f"Помилка: невідоме today_exercise '{today_exercise}' (очікується 'pushups' або 'squats')."
                effective_exercise = today_exercise
                if effective_exercise is None:
                    effective_exercise = before_exercise if before_mode == "alternate" else "pushups"
                no_op = challenge.enable_alternating(
                    store, today, challenge.pushups_equivalent(store, today), squat_factor, effective_exercise
                )
            elif mode == "ladder":
                no_op = None
                challenge.disable_alternating(store, today)
            else:
                return f"Помилка: невідомий режим '{mode}'."

            after_mode = store.get_state("schedule_mode", "ladder")
            after_exercise = challenge.day_exercise(store, today)
            after_targets = challenge.day_targets(store, today)
            logger.info(
                "set_schedule state change: mode %s->%s, today(%s) exercise %s->%s, targets %s->%s",
                before_mode, after_mode, today.isoformat(), before_exercise, after_exercise,
                before_targets, after_targets,
            )

            if no_op:
                actions.append(f"set_schedule(alternate, no_op={no_op})")
                return f"Розклад уже 'alternate' ({no_op}). {_today_tomorrow_summary()}"

            actions.append(f"set_schedule({mode}, {squat_factor}, today_exercise={today_exercise})")
            return f"Розклад: {mode}. {_today_tomorrow_summary()}"

        def swap_today_exercise() -> str:
            """
            Перемкнути вправу СЬОГОДНІ на протилежну в межах чинного чергування (віджимання <-> присідання),
            не зачіпаючи саму систему 'alternate' — завтрашній день далі просувається природно.
            Застосовуй САМЕ цей інструмент для прохань типу «сьогодні навпаки» / «поміняй вправу
            сьогодні» / «зробимо сьогодні ноги» БЕЗ називання конкретної вправи — він сам визначить
            протилежну до поточної. Якщо адмін називає конкретну вправу («сьогодні точно присідання») —
            краще використати set_schedule(mode='alternate', today_exercise=...).
            Вимагає, щоб розклад уже був 'alternate' (інакше спершу set_schedule(mode='alternate', ...)).
            """
            before_mode = store.get_state("schedule_mode", "ladder")
            if before_mode != "alternate":
                return "Помилка: розклад зараз 'ladder' (не чергування) — спершу set_schedule(mode='alternate', ...)."

            before_exercise = challenge.day_exercise(store, today)
            target_exercise = "squats" if before_exercise == "pushups" else "pushups"
            factor = float(store.get_state("alt_squat_factor", "1.5"))
            pushups_map = challenge.pushups_equivalent(store, today)

            challenge.enable_alternating(store, today, pushups_map, factor, target_exercise)

            after_exercise = challenge.day_exercise(store, today)
            logger.info(
                "swap_today_exercise state change: today(%s) exercise %s->%s",
                today.isoformat(), before_exercise, after_exercise,
            )
            actions.append(f"swap_today_exercise({before_exercise}->{after_exercise})")
            return f"Вправу сьогодні змінено: {before_exercise} -> {after_exercise}. {_today_tomorrow_summary()}"

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
            if not _KEY_RE.match(key) or key in PROTECTED_KEYS or key.startswith(SCHEDULE_KEY_PREFIXES):
                return f"Помилка: ключ '{key}' недопустимий."
            before = store.get_state(key, "")
            store.set_state(key, value)
            logger.info("set_counter state change: %s: %r -> %r", key, before, value)
            actions.append(f"set_counter({key}={value})")
            return f"Лічильник {key} = {value} ({description})."

        def update_coach_prompt(rule: str, original_quote: str, valid_until: str = "") -> str:
            """
            Змінити поведінку, стиль, звертання, титули чи правила спілкування тренера.
            За замовчуванням (якщо Grok Box доступний) офлоадить на box для створення git-міграції.
            Якщо box недоступний — аварійний офлайн-fallback у локальну БД (updated_by='bot_fallback').
            :param rule: одне атомарне правило — повне, однозначне, від третьої особи («Льошу називати …»).
            :param original_quote: ДОСЛІВНА цитата наказу з чату (з усіма назвами, титулами й числами як є).
            :param valid_until: '' — постійне правило; або дата YYYY-MM-DD, до якої правило діє включно.
            """
            change = RuleChange(rule=rule, quote=original_quote, until=valid_until,
                                author=author, issued=today.isoformat())

            if self.is_box_available():
                import uuid
                request_id = str(uuid.uuid4())
                payload = {
                    "action": "update_coach_prompt",
                    "rule": rule,
                    "original_quote": original_quote,
                    "valid_until": valid_until,
                    "author": author,
                    "issued": today.isoformat(),
                }
                store.create_offload_request(
                    request_id=request_id,
                    type="admin",
                    author_slug=author,
                    payload=payload,
                    status="sent",
                )
                actions.append(f"update_coach_prompt(offload_to_box, request_id={request_id})")
                return (
                    f"Запит на зміну правил тренера передано на Grok Box (admin_dev, request_id={request_id}). "
                    f"Зміни буде внесено через git-міграцію."
                )

            # Fallback у разі недоступності box: пишемо в локальну БД з updated_by='bot_fallback'
            result = editor.apply(change, updated_by="bot_fallback")
            actions.append(f"update_coach_prompt(fallback=bot_fallback, strategy={result.strategy}, v{result.version})")
            if not result.changed:
                return f"Правило вже діє, змін не потрібно ({result.reason})."
            return f"Grok Box недоступний — правило застосовано локально (bot_fallback, версія {result.version}). Позначено для reconcile."

        def reset_coach_prompt() -> str:
            """Скинути всі динамічні правила тренера до порожніх (лишається тільки базова роль)."""
            if self.is_box_available():
                import uuid
                request_id = str(uuid.uuid4())
                payload = {"action": "reset_coach_prompt", "author": author, "issued": today.isoformat()}
                store.create_offload_request(
                    request_id=request_id,
                    type="admin",
                    author_slug=author,
                    payload=payload,
                    status="sent",
                )
                actions.append(f"reset_coach_prompt(offload_to_box, request_id={request_id})")
                return f"Запит на скидання динамічних правил передано на Grok Box (admin_dev, request_id={request_id})."

            version = editor.reset(author, updated_by="bot_fallback")
            actions.append(f"reset_coach_prompt(fallback=bot_fallback, v{version})")
            return f"Grok Box недоступний — динамічні правила скинуто локально (bot_fallback, версія {version})."

        def show_coach_prompt() -> str:
            """Показати чинні динамічні правила тренера."""
            return store.get_prompt(COACH_DYNAMIC_KEY) or "(динамічних правил немає)"

        def set_bot_text(key: str, text: str) -> str:
            """
            Змінити фіксований текст відповіді бота (bot_texts) за ключем — на відміну від
            update_coach_prompt, це ДАНІ (конкретна фраза), а не правило поведінки; діє негайно,
            без деплою й без рестарту бота.
            :param key: ключ тексту в snake_case (наприклад 'angle_hint', 'success', 'near_miss',
                        'far_below', 'bad_quality', 'wrong_exercise', 'processing').
            :param text: новий текст. Для 'success' підтримуються плейсхолдери {reps} і {target}.
            """
            if not _KEY_RE.match(key):
                return f"Помилка: ключ '{key}' недопустимий."
            bot_texts.set(key, text, updated_by=author)
            actions.append(f"set_bot_text({key})")
            return f"Текст '{key}' оновлено: «{text}»."

        def create_admin_task(text: str) -> str:
            """
            Передати запит розробнику, якщо наказ НЕ покривається жодним іншим тулом (нова фіча, зміна логіки,
            правила, багфікс тощо). НЕ імпровізуй і не вдавай, що зробив — виклич цей тул.
            :param text: короткий, чіткий опис запиту українською (що саме треба змінити і навіщо).
            """
            from core.config import CHAT_ID
            user = store.find_user(author) if author else None
            task_id = store.create_admin_task(
                CHAT_ID or None,
                user.telegram_id if user else None,
                (user.display_name if user else author) or "невідомо",
                (text or "").strip()[:2000],
            )
            actions.append(f"create_admin_task(#{task_id})")
            return bot_texts.get("admin_task_created", id=task_id)

        def list_bot_texts() -> str:
            """Отримати всі фіксовані тексти бота (bot_texts): ключ, поточний текст, хто востаннє редагував."""
            actions.append("list_bot_texts()")
            return json.dumps(store.list_bot_texts(), ensure_ascii=False)

        return [
            _logged_tool(t) for t in (
                set_lives, set_targets, set_schedule, swap_today_exercise, set_progress, update_user_profile,
                get_chat_members_info, set_counter, update_coach_prompt, reset_coach_prompt, show_coach_prompt,
                set_bot_text, list_bot_texts, create_admin_task,
            )
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
