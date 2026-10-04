"""
Доменна логіка челенджу: норми, вправа дня, статус, життя. Без жодного AI.

Два режими розкладу (bot_state.schedule_mode):
  ladder    — «Драбинка»: щодня віджимання, база + номер дня + offset_<p>.
  alternate — з alt_start_date чергуються дні: віджимання ↔ присідання.
              Віджимання: alt_pushups_<p> + 1 за кожен попередній день віджимань.
              Присідання: ceil(alt_squat_factor × учорашні віджимання) — дробових норм не буває.
Разова правка будь-якої норми на конкретний день: override_<p>_<YYYY-MM-DD>.
"""
import math
from datetime import date, timedelta
from typing import Dict, List, Optional

from core.config import START_DATE
from database.store import Store

PARTICIPANTS = {
    "lyosha": {"name": "Льоша", "base": 3, "emoji": "👦"},
    "sasha": {"name": "Саша", "base": 13, "emoji": "👨"},
}

_ALIASES = {
    "lyosha": ["льош", "леш", "lyosha", "alexey", "oleksiy", "олекс", "алекс", "син", "малий", "сонцелик"],
    "sasha": ["саш", "саня", "sasha", "oleksandr", "олександр", "александр", "тато", "батько", "polishchuk"],
}

EXERCISES = {
    "pushups": {"name": "віджимання", "genitive": "віджимань", "emoji": "💪"},
    "squats": {"name": "присідання", "genitive": "присідань", "emoji": "🦵"},
}

LADDER_RULES = (
    "📋 Правила челенджу «Драбинка (+1 щодня)»:\n"
    "1. Старт: 31.08.2026.\n"
    "2. Базові норми (День 0): Льоша — 3, Саша — 13 віджимань.\n"
    "3. Щодня норма для обох зростає на +1.\n"
    "4. Обидва учасники мають щодня закрити свою норму до сну.\n"
    "5. Звітувати можна відео, кружечками або будь-якими повідомленнями в чат!"
)


def rules_text(store: Store) -> str:
    if store.get_state("schedule_mode", "ladder") != "alternate":
        return LADDER_RULES
    factor = store.get_state("alt_squat_factor", "1.5")
    start = date.fromisoformat(store.get_state("alt_start_date"))
    return (
        "📋 Правила челенджу (система чергування):\n"
        f"1. З {start.strftime('%d.%m.%Y')} дні чергуються: день віджимань ↔ день присідань.\n"
        "2. Віджимання: +1 від останнього дня віджимань.\n"
        f"3. Присідання: у {factor} раза більше, ніж віджимань учора (округлення вгору).\n"
        "4. Обидва учасники мають щодня закрити свою норму до сну.\n"
        "5. За незакритий день — мінус 1 життя.\n"
        "6. Звітувати можна відео, кружечками або будь-якими повідомленнями в чат!"
    )


def normalize_participant(raw: str) -> Optional[str]:
    """«Олександр», «тато», «sasha» -> 'sasha'. Повертає None, якщо не впізнано."""
    value = (raw or "").lower().strip()
    if value in PARTICIPANTS:
        return value
    # «олекс» входить і в «олександр», тому спершу перевіряємо Сашині псевдоніми.
    for key in ("sasha", "lyosha"):
        if any(alias in value for alias in _ALIASES[key]):
            return key
    return None


def day_index(day: date) -> int:
    return max(0, (day - START_DATE).days)


def _alternate_index(store: Store, day: date) -> Optional[int]:
    """Номер дня від старту системи чергування або None, якщо для цієї дати діє «Драбинка»."""
    if store.get_state("schedule_mode", "ladder") != "alternate":
        return None
    start = date.fromisoformat(store.get_state("alt_start_date"))
    return (day - start).days if day >= start else None


def day_exercise(store: Store, day: date) -> str:
    k = _alternate_index(store, day)
    return "squats" if k is not None and k % 2 == 1 else "pushups"


def _formula_target(store: Store, participant: str, day: date) -> int:
    k = _alternate_index(store, day)
    if k is None:
        offset = int(store.get_state(f"offset_{participant}", "0") or 0)
        return PARTICIPANTS[participant]["base"] + day_index(day) + offset
    # Для дня присідань k // 2 == кількість днів віджимань до вчора, тобто це саме вчорашні віджимання.
    pushups = int(store.get_state(f"alt_pushups_{participant}")) + k // 2
    if k % 2 == 0:
        return pushups
    return math.ceil(float(store.get_state("alt_squat_factor", "1.5")) * pushups)


def day_targets(store: Store, day: date) -> Dict[str, int]:
    targets = {}
    for p in PARTICIPANTS:
        override = store.get_state(f"override_{p}_{day.isoformat()}", "")
        targets[p] = max(0, int(override) if override.strip() else _formula_target(store, p, day))
    return targets


def set_target(store: Store, participant: str, target: int, day: date) -> None:
    """
    Встановити норму на `day`.
    День віджимань: зсуваємо формулу, тож далі віджимання підуть +1 від нового значення
    (а присідання наступного дня — від нього ж × коефіцієнт).
    День присідань: разова правка лише цього дня (наступні віджимання не змінюються).
    """
    k = _alternate_index(store, day)
    if k is None:
        base = PARTICIPANTS[participant]["base"] + day_index(day)
        store.set_state(f"offset_{participant}", str(target - base))
    elif k % 2 == 0:
        store.set_state(f"alt_pushups_{participant}", str(target - k // 2))
    else:
        store.set_state(f"override_{participant}_{day.isoformat()}", str(target))


def enable_alternating(store: Store, start: date, pushups: Dict[str, int], squat_factor: float = 1.5) -> None:
    """Увімкнути систему чергування: `start` — день віджимань із нормами `pushups`."""
    for p in PARTICIPANTS:
        store.set_state(f"alt_pushups_{p}", str(int(pushups[p])))
    store.set_state("alt_squat_factor", str(squat_factor))
    store.set_state("alt_start_date", start.isoformat())
    store.set_state("schedule_mode", "alternate")


def disable_alternating(store: Store, day: date) -> None:
    """Повернутися до «Драбинки», зберігши сьогоднішню норму віджимань як точку відліку."""
    current = {p: _formula_target(store, p, day) for p in PARTICIPANTS} if day_exercise(store, day) == "pushups" else None
    store.set_state("schedule_mode", "ladder")
    if current:
        for p, value in current.items():
            set_target(store, p, value, day)


def get_lives(store: Store) -> Dict[str, Optional[int]]:
    lives = {}
    for p in PARTICIPANTS:
        raw = store.get_state(f"lives_{p}", "")
        lives[p] = int(raw) if raw.strip().lstrip("-").isdigit() else None
    return lives


def status_snapshot(store: Store, day: date) -> Dict:
    targets = day_targets(store, day)
    done = dict(zip(("lyosha", "sasha"), store.get_progress(day.isoformat())))
    exercise = day_exercise(store, day)
    return {
        "exercise": exercise,
        "exercise_name": EXERCISES[exercise]["name"],
        "date": day.strftime("%d.%m.%Y"),
        "date_iso": day.isoformat(),
        "day_num": day_index(day) + 1,
        "targets": targets,
        "done": done,
        "lives": get_lives(store),
    }


def status_text(store: Store, day: date) -> str:
    s = status_snapshot(store, day)
    lines = [f"Дата: {s['date']} (День {s['day_num']} челенджу). Вправа дня: {s['exercise_name'].upper()}."]
    for p, meta in PARTICIPANTS.items():
        mark = "ВИКОНАНО ✅" if s["done"][p] else "НЕ виконано ❌"
        lives = s["lives"][p]
        lives_str = f" | Життя: {lives}" if lives is not None else ""
        lines.append(f"{meta['name']}: норма {s['targets'][p]} | Статус: {mark}{lives_str}")
    return "\n".join(lines)


def missing_participants(store: Store, day: date) -> List[str]:
    s = status_snapshot(store, day)
    return [p for p in PARTICIPANTS if not s["done"][p]]


def settle_missed_day(store: Store, day: date) -> Dict[str, int]:
    """
    Знімає 1 життя кожному, хто не закрив норму за `day` (лише якщо лічильник життів існує).
    Ідемпотентно: дата останнього розрахунку зберігається в bot_state.lives_settled_through.
    Повертає {participant: нове_значення} для тих, у кого списали життя.
    """
    day_iso = day.isoformat()
    if store.get_state("lives_settled_through", "") >= day_iso:
        return {}
    lives = get_lives(store)
    done = dict(zip(("lyosha", "sasha"), store.get_progress(day_iso)))
    changed = {}
    for p in PARTICIPANTS:
        if lives[p] is not None and not done[p]:
            changed[p] = max(0, lives[p] - 1)
            store.set_state(f"lives_{p}", str(changed[p]))
    store.set_state("lives_settled_through", day_iso)
    return changed


def yesterday(today: date) -> date:
    return today - timedelta(days=1)
