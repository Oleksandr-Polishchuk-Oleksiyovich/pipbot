"""
Доменна логіка челенджу: норми, вправа дня, статус, життя. Без жодного AI.
Працює з динамічним списком учасників із таблиці `users` у БД.

Два режими розкладу (bot_state.schedule_mode):
  ladder    — «Драбинка»: щодня віджимання, base_pushups + номер дня + ladder_offset.
  alternate — з alt_start_date чергуються дні: віджимання ↔ присідання.
              Віджимання: alt_pushups + 1 за кожен попередній день віджимань.
              Присідання: ceil(alt_squat_factor × учорашні віджимання) — дробових норм не буває.
Разова правка будь-якої норми на конкретний день: override_<slug>_<YYYY-MM-DD>.
"""
import logging
import math
from datetime import date, timedelta
from typing import Dict, List, Optional

from core.config import DB_PATH, IS_PRODUCTION, START_DATE
from database.store import DIFFICULTY_LIVES, Store, User
from migrate_users import ensure_default_users

logger = logging.getLogger(__name__)

EXERCISES = {
    "pushups": {"name": "віджимання", "genitive": "віджимань", "emoji": "💪",
                "forms": ("віджимання", "віджимання", "віджимань")},
    "squats": {"name": "присідання", "genitive": "присідань", "emoji": "🦵",
               "forms": ("присідання", "присідання", "присідань")},
}

# CV-класи (video_checks.exercise) -> ключі EXERCISES
CV_EXERCISE_KEYS = {"pushup": "pushups", "pushups": "pushups", "squat": "squats", "squats": "squats"}


def plural_uk(n: int, forms) -> str:
    """Українська форма після числа: forms = (1, 2–4, 5+), напр. 1 присідання, 3 присідання, 5 присідань."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def exercise_word(exercise_key: str, n: int) -> str:
    """Назва вправи у правильній формі для числа n (exercise_key: 'pushups'/'squats' або CV-клас)."""
    key = CV_EXERCISE_KEYS.get(exercise_key or "", exercise_key)
    info = EXERCISES.get(key) or EXERCISES["pushups"]
    return plural_uk(n, info["forms"])


def _active_users(store: Store) -> List[User]:
    users = store.list_users(status="active")
    if not users and not store.list_users():
        if IS_PRODUCTION:
            logger.error(
                "Users table is empty in production DB (%s) — refusing to seed default "
                "participants automatically. Run migrate_users.py explicitly or restore from backup.",
                DB_PATH,
            )
        else:
            ensure_default_users(store)
            users = store.list_users(status="active")
    return users


def participants_map(store: Store) -> Dict[str, dict]:
    """Словник активних учасників {slug: метадані} із таблиці users."""
    return {
        u.slug: {
            "name": u.display_name,
            "telegram_name": u.telegram_name,
            "base": u.base_pushups,
            "emoji": u.emoji,
            "description": u.description,
            "difficulty": u.difficulty,
            "lives": u.lives,
            "max_lives": u.max_lives,
        }
        for u in _active_users(store)
    }


def rules_text(store: Store) -> str:
    active = _active_users(store)
    names = ", ".join(f"{u.emoji} {u.display_name}" for u in active) or "учасники чату"
    if store.get_state("schedule_mode", "ladder") != "alternate":
        return (
            "📋 Правила челенджу «Драбинка (+1 щодня)»:\n"
            f"1. Старт челенджу: 31.08.2026. Активні учасники: {names}.\n"
            "2. Кожен учасник має власну стартову норму та режим життів (Хардкор — 2, Норма — 5, Вітальний — 10).\n"
            "3. Щодня норма для кожного зростає на +1.\n"
            "4. Усі учасники мають щодня закрити свою норму до півночі (інакше -1 життя).\n"
            "5. Звітувати можна відео, кружечками або повідомленнями в чат!"
        )
    factor = store.get_state("alt_squat_factor", "1.5")
    start = date.fromisoformat(store.get_state("alt_start_date"))
    return (
        "📋 Правила челенджу (система чергування):\n"
        f"1. З {start.strftime('%d.%m.%Y')} дні чергуються: день віджимань ↔ день присідань.\n"
        f"2. Активні учасники: {names}.\n"
        "3. Віджимання: +1 від останнього дня віджимань.\n"
        f"4. Присідання: у {factor} раза більше, ніж віджимань учора (округлення вгору).\n"
        "5. Усі активні учасники мають щодня закрити свою норму до півночі.\n"
        "6. За незакритий день — мінус 1 життя (режими для новачків: Хардкор — 2, Норма — 5, Вітальний — 10).\n"
        "7. Звітувати можна відео, кружечками або будь-якими повідомленнями в чат!"
    )


def normalize_participant(raw: str, store: Optional[Store] = None) -> Optional[str]:
    """Розпізнає учасника за ім'ям, ніком, slug або псевдонімом із БД (users)."""
    value = (raw or "").lower().strip()
    if not value:
        return None
    if store is not None:
        _active_users(store)
        user = store.find_user(raw)
        if user is not None:
            return user.slug
    return None


def day_index(day: date) -> int:
    return max(0, (day - START_DATE).days)


def _alternate_index(store: Store, day: date) -> Optional[int]:
    """Номер дня від старту системи чергування або None, якщо для цієї дати діє «Драбинка»."""
    if store.get_state("schedule_mode", "ladder") != "alternate":
        return None
    raw_start = store.get_state("alt_start_date", "")
    if not raw_start:
        return None
    start = date.fromisoformat(raw_start)
    return (day - start).days if day >= start else None


def day_exercise(store: Store, day: date) -> str:
    k = _alternate_index(store, day)
    return "squats" if k is not None and k % 2 == 1 else "pushups"


def _alt_pushups_base(store: Store, user: User, day: date, k: int) -> int:
    """Пушапс-еквівалент `day` у системі чергування (базове число до коефіцієнта присідань)."""
    alt_base = user.alt_pushups
    if alt_base is None:
        raw_state = store.get_state(f"alt_pushups_{user.slug}", "")
        if raw_state.strip().lstrip("-").isdigit():
            alt_base = int(raw_state)
        else:
            alt_start = date.fromisoformat(store.get_state("alt_start_date"))
            alt_base = user.base_pushups + day_index(alt_start) + user.ladder_offset
    return alt_base + k // 2


def _formula_target_for_user(store: Store, user: User, day: date) -> int:
    k = _alternate_index(store, day)
    if k is None:
        return user.base_pushups + day_index(day) + user.ladder_offset
    pushups = _alt_pushups_base(store, user, day, k)
    if k % 2 == 0:
        return pushups
    return math.ceil(float(store.get_state("alt_squat_factor", "1.5")) * pushups)


def pushups_equivalent(store: Store, day: date) -> Dict[str, int]:
    """
    Пушапс-еквівалент норми на `day` для кожного активного учасника, незалежно від того, яка
    вправа сьогодні фактично: у режимі 'ladder' це просто норма дня; у режимі 'alternate' —
    базове число віджимань (до коефіцієнта присідань), з якого однаково обчислюються і
    віджимання, і присідання. Використовується, щоб коректно перемикати вправу дня
    (`enable_alternating(..., today_exercise=...)`, `swap_today_exercise`) без спотворення прогресії.
    """
    result = {}
    for u in _active_users(store):
        k = _alternate_index(store, day)
        result[u.slug] = _formula_target_for_user(store, u, day) if k is None else _alt_pushups_base(store, u, day, k)
    return result


def day_targets(store: Store, day: date) -> Dict[str, int]:
    targets = {}
    for u in _active_users(store):
        override = store.get_state(f"override_{u.slug}_{day.isoformat()}", "")
        if override.strip():
            value = int(override)
        else:
            nn = store.get_next_norm(u.slug, day_exercise(store, day))
            # Адаптивна норма (core/progression.py) діє з effective_from для днів тієї ж вправи.
            value = nn[0] if nn and day.isoformat() >= nn[1] else _formula_target_for_user(store, u, day)
        targets[u.slug] = max(0, value)
    return targets


def set_target(store: Store, participant: str, target: int, day: date) -> None:
    """
    Встановити норму для `participant` на `day`.
    День віджимань: зсуваємо формулу, тож далі віджимання підуть +1 від нового значення.
    День присідань: разова правка лише цього дня (наступні віджимання не змінюються).
    """
    _active_users(store)
    user = store.get_user(participant) or store.find_user(participant)
    if user is None:
        raise ValueError(f"Unknown participant: {participant}")
    k = _alternate_index(store, day)
    # Адмінська норма перекриває адаптивну: прибираємо next_norms цієї вправи — сьогодні діє формула/override
    # з новим значенням, а о 00:05 наступна норма порахується вже від нього (core/progression.py).
    store.clear_next_norm(user.slug, day_exercise(store, day))
    if k is None:
        base = user.base_pushups + day_index(day)
        user.ladder_offset = int(target) - base
        store.upsert_user(user)
        store.set_state(f"offset_{user.slug}", str(user.ladder_offset))
    elif k % 2 == 0:
        user.alt_pushups = int(target) - k // 2
        store.upsert_user(user)
        store.set_state(f"alt_pushups_{user.slug}", str(user.alt_pushups))
    else:
        store.set_state(f"override_{user.slug}_{day.isoformat()}", str(int(target)))


def set_starting_pushups(store: Store, participant: str, starting_pushups: int, day: date) -> None:
    """
    Фіксує стартову норму віджимань для нового учасника на дату приєднання `day`
    незалежно від того, чи сьогодні день віджимань чи присідань.
    """
    user = store.get_user(participant) or store.find_user(participant)
    if user is None:
        raise ValueError(f"Unknown participant: {participant}")
    start_val = max(1, int(starting_pushups))
    user.base_pushups = start_val
    user.ladder_offset = -day_index(day)
    k = _alternate_index(store, day)
    if k is not None:
        user.alt_pushups = start_val - (k // 2)
        store.set_state(f"alt_pushups_{user.slug}", str(user.alt_pushups))
    store.upsert_user(user)
    store.set_state(f"offset_{user.slug}", str(user.ladder_offset))


def enable_alternating(
    store: Store,
    start: date,
    pushups: Dict[str, int],
    squat_factor: float = 1.5,
    today_exercise: str = "pushups",
) -> Optional[str]:
    """
    Увімкнути систему чергування з дня `start`.

    `today_exercise` визначає, яка вправа припадає на `start` ('pushups' — за замовчуванням, або 'squats'):
    - 'pushups': `start` = alt_start_date, норми `pushups` діють на `start` як є.
    - 'squats': alt_start_date = `start` - 1 день, тож `start` стає днем присідань
      (ceil(squat_factor × `pushups`), округлення вгору), а наступний день віджимань = `pushups` + 1.
      `pushups` тут має бути пушапс-еквівалентом дня (див. `pushups_equivalent`), а не вже відображеною
      нормою — інакше число спотвориться.

    Якщо режим уже 'alternate' і вправа дня `start` за чинним розкладом вже збігається з `today_exercise` —
    нічого не змінює (щоб не збити лічильник прогресії) і повертає рядок із поясненням no-op.
    Повертає None, якщо зміну застосовано.
    """
    if today_exercise not in ("pushups", "squats"):
        raise ValueError(f"Unknown today_exercise: {today_exercise!r} (expected 'pushups' or 'squats')")

    if store.get_state("schedule_mode", "ladder") == "alternate" and day_exercise(store, start) == today_exercise:
        return (
            f"уже 'alternate', і {start.isoformat()} — день '{today_exercise}' за чинним розкладом; без змін"
        )

    alt_start = start if today_exercise == "pushups" else start - timedelta(days=1)
    for u in _active_users(store):
        val = int(pushups.get(u.slug, _formula_target_for_user(store, u, start)))
        u.alt_pushups = val
        store.upsert_user(u)
        store.set_state(f"alt_pushups_{u.slug}", str(val))
    store.set_state("alt_squat_factor", str(squat_factor))
    store.set_state("alt_start_date", alt_start.isoformat())
    store.set_state("schedule_mode", "alternate")
    return None


def disable_alternating(store: Store, day: date) -> None:
    """Повернутися до «Драбинки», зберігши сьогоднішню норму віджимань як точку відліку."""
    users = _active_users(store)
    current = {u.slug: _formula_target_for_user(store, u, day) for u in users} if day_exercise(store, day) == "pushups" else None
    store.set_state("schedule_mode", "ladder")
    if current:
        for slug, value in current.items():
            set_target(store, slug, value, day)


def get_lives(store: Store) -> Dict[str, Optional[int]]:
    lives = {}
    for u in _active_users(store):
        if u.lives is not None:
            lives[u.slug] = u.lives
        else:
            raw = store.get_state(f"lives_{u.slug}", "")
            lives[u.slug] = int(raw) if raw.strip().lstrip("-").isdigit() else None
    return lives


def get_max_lives(store: Store) -> Dict[str, Optional[int]]:
    return {u.slug: u.max_lives for u in _active_users(store)}


def set_user_lives(store: Store, participant: str, lives: int, max_lives: Optional[int] = None) -> User:
    _active_users(store)
    user = store.get_user(participant) or store.find_user(participant)
    if user is None:
        raise ValueError(f"Unknown participant: {participant}")
    user.lives = max(0, int(lives))
    if max_lives is not None:
        user.max_lives = max(1, int(max_lives))
    elif user.max_lives is not None and user.lives > user.max_lives:
        user.max_lives = user.lives
    store.upsert_user(user)
    store.set_state(f"lives_{user.slug}", str(user.lives))
    return user


def recommended_start_reps(max_reps: int) -> int:
    """Пропонована стартова норма — третина від максимуму за раз (округлення вгору, мінімум 1)."""
    return max(1, math.ceil(int(max_reps) / 3.0))


def status_snapshot(store: Store, day: date) -> Dict:
    users = _active_users(store)
    uninitialized = store.list_users(status="uninitialized")
    targets = day_targets(store, day)
    done = store.get_day_progress(day.isoformat())
    lives = get_lives(store)
    max_lives = get_max_lives(store)
    exercise = day_exercise(store, day)
    participants_info = {}
    day_iso = day.isoformat()
    for u in users:
        leave_rec = store.get_leave_on(u.slug, day_iso)
        leave_info = (
            {
                "kind": leave_rec["kind"],
                "until": leave_rec["end_date"],
                "set_by": leave_rec.get("set_by"),
            }
            if leave_rec
            else None
        )
        participants_info[u.slug] = {
            "name": u.display_name,
            "telegram_name": u.telegram_name,
            "emoji": u.emoji,
            "description": u.description,
            "difficulty": u.difficulty,
            "target": targets.get(u.slug, 0),
            "done": done.get(u.slug, False),
            "lives": lives.get(u.slug),
            "max_lives": max_lives.get(u.slug),
            "xp": store.get_xp(u.slug)[0],
            "level": store.get_xp(u.slug)[1],
            "leave": leave_info,
        }
    return {
        "exercise": exercise,
        "exercise_name": EXERCISES[exercise]["name"],
        "date": day.strftime("%d.%m.%Y"),
        "date_iso": day.isoformat(),
        "day_num": day_index(day) + 1,
        "targets": targets,
        "done": done,
        "lives": lives,
        "max_lives": max_lives,
        "participants": participants_info,
        "uninitialized": [
            {
                "slug": u.slug,
                "display_name": u.display_name,
                "telegram_name": u.telegram_name,
                "description": u.description,
                "max_pushups_reported": u.max_pushups_reported,
                "max_squats_reported": u.max_squats_reported,
                "difficulty": u.difficulty,
            }
            for u in uninitialized
        ],
    }


def status_text(store: Store, day: date) -> str:
    s = status_snapshot(store, day)
    lines = [f"Дата: {s['date']} (День {s['day_num']} челенджу). Вправа дня: {s['exercise_name'].upper()}."]
    for slug, info in s["participants"].items():
        if info["done"]:
            mark = "ВИКОНАНО ✅"
        elif info.get("leave"):
            kind = info["leave"]["kind"]
            until = info["leave"].get("until")
            until_str = ""
            if until:
                try:
                    d_until = date.fromisoformat(str(until)[:10])
                    until_str = f" (до {d_until.strftime('%d.%m')})"
                except Exception:
                    until_str = f" (до {until})"
            if kind == "sick":
                mark = f"🤒 на лікарняному{until_str}"
            else:
                mark = f"🏖 у відпустці{until_str}"
        else:
            mark = "НЕ виконано ❌"
        lives = info["lives"]
        max_l = info["max_lives"]
        if lives is not None:
            lives_str = f" | Життя: {lives}/{max_l}" if max_l else f" | Життя: {lives}"
        else:
            lives_str = ""
        lines.append(f"{info['emoji']} {info['name']}: норма {info['target']} | Статус: {mark}{lives_str}"
                     f" | XP {info.get('xp', 0)}/100 | Рівень {info.get('level', 0)}")
    return "\n".join(lines)


def missing_participants(store: Store, day: date) -> List[str]:
    s = status_snapshot(store, day)
    return [
        slug for slug, done in s["done"].items()
        if not done and not s["participants"].get(slug, {}).get("leave")
    ]


def settle_missed_day(store: Store, day: date) -> Dict[str, int]:
    """
    Знімає 1 життя кожному активному учаснику, хто був у челенджі на дату `day` (joined_date <= day)
    і не закрив норму (лише якщо лічильник життів існує).
    Ідемпотентно: дата останнього розрахунку зберігається в bot_state.lives_settled_through.
    Повертає {slug: нове_значення} для тих, у кого списали життя.
    """
    day_iso = day.isoformat()
    if store.get_state("lives_settled_through", "") >= day_iso:
        return {}
    users = _active_users(store)
    lives = get_lives(store)
    done = store.get_day_progress(day_iso)
    changed = {}
    for u in users:
        if u.joined_date and u.joined_date > day_iso:
            continue
        cur_lives = lives.get(u.slug)
        if cur_lives is not None and not done.get(u.slug, False):
            if store.get_leave_on(u.slug, day_iso):
                continue
            new_val = max(0, cur_lives - 1)
            changed[u.slug] = new_val
            set_user_lives(store, u.slug, new_val)
    store.set_state("lives_settled_through", day_iso)
    return changed


def yesterday(today: date) -> date:
    return today - timedelta(days=1)
