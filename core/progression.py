"""
Прогресія (наказ Саші 2026-10-22): адаптивна наступна норма, найкраща спроба дня, XP і рівні.

Формули (цілочисельні; ділення — floor, тобто `//`):
- Залік:            next = (done + target) // 2 + 1,  де done = max(найкраща спроба, target)
                    (зарахування без числа — текстом/адміном — вважаємо done = target).
- Не залік, була спроба (best > 0):   next = max(1, best - 5)
- Не робив зовсім:                     next = max(1, target - 10)
Наступна норма діє для НАСТУПНОГО дня тієї ж вправи (віджимання/присідання — окремо).

XP: залік = +1, плюс +2 за кожне повторення понад норму. Не залік = 0.
Кожні 100 XP — левел-ап: max_lives += 1, поточні життя = max_lives. Залишок понад 100 переноситься
(95 + 7 -> рівень +1, 2 XP). Якщо в учасника немає лічильника життів (max_lives NULL) — лише рівень.
"""
import logging
from datetime import date, timedelta
from typing import Dict, Optional, Tuple

from database.store import Store

logger = logging.getLogger(__name__)

XP_PER_LEVEL = 100


def next_norm(target: int, credited: bool, best: Optional[int]) -> int:
    target = int(target)
    if credited:
        done = max(int(best or 0), target)
        return (done + target) // 2 + 1
    if best and best > 0:
        return max(1, int(best) - 5)
    return max(1, target - 10)


def xp_gain(target: int, credited: bool, best: Optional[int]) -> int:
    if not credited:
        return 0
    return 1 + 2 * max(0, int(best or 0) - int(target))


def apply_xp(xp: int, level: int, gain: int) -> Tuple[int, int, int]:
    """(new_xp, new_level, levels_gained); залишок понад 100 переноситься."""
    xp, level, ups = int(xp) + int(gain), int(level), 0
    while xp >= XP_PER_LEVEL:
        xp -= XP_PER_LEVEL
        level += 1
        ups += 1
    return xp, level, ups


def _best_from_video_checks(store: Store, slug: str, day: date, exercise: str) -> Optional[int]:
    """Фолбек: найкраща спроба з video_checks (для днів до появи daily_attempts). Вправа має збігатися."""
    from core import challenge
    from core.config import TIMEZONE as TZ
    try:
        from datetime import datetime, timezone
        since = (datetime.combine(day, datetime.min.time()) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        best = None
        for created, ex, reps in store.video_reps_rows(slug, since):
            local = datetime.strptime(created[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(TZ)
            if local.date() == day and challenge.CV_EXERCISE_KEYS.get(ex or "") == exercise:
                best = max(best or 0, reps)
        return best
    except Exception as e:  # noqa: BLE001
        logger.warning("video_checks fallback failed for %s: %s", slug, e)
        return None


def settle_progression(store: Store, day: date) -> Dict[str, Dict]:
    """
    Розрахунок за `day` (викликається о 00:05 за вчора, ПІСЛЯ списання життів — левел-ап відновлює життя).
    Ідемпотентно через bot_state.progression_settled_through.
    """
    from core import challenge

    day_iso = day.isoformat()
    if store.get_state("progression_settled_through", "") >= day_iso:
        return {}
    exercise = challenge.day_exercise(store, day)
    targets = challenge.day_targets(store, day)
    done = store.get_day_progress(day_iso)
    result: Dict[str, Dict] = {}
    for u in challenge._active_users(store):
        if u.joined_date and u.joined_date > day_iso:
            continue
        if u.slug not in targets:
            continue
        target = targets[u.slug]
        credited = bool(done.get(u.slug, False))
        best = store.get_best_attempt(day_iso, u.slug)
        if best is None:
            best = _best_from_video_checks(store, u.slug, day, exercise)
        nxt = next_norm(target, credited, best)
        store.set_next_norm(u.slug, exercise, nxt, (day + timedelta(days=1)).isoformat(), day_iso)
        gain = xp_gain(target, credited, best)
        xp, level = store.get_xp(u.slug)
        xp, level, ups = apply_xp(xp, level, gain)
        store.set_xp(u.slug, xp, level)
        if gain:
            store.add_xp_log(day_iso, u.slug, gain, f"target={target} best={best} credited={credited}")
        user = store.get_user(u.slug) or u
        lives, max_lives = user.lives, user.max_lives
        if ups and max_lives is not None:
            max_lives += ups
            lives = max_lives
            challenge.set_user_lives(store, u.slug, lives, max_lives)
        result[u.slug] = {"exercise": exercise, "target": target, "credited": credited, "best": best,
                          "next": nxt, "xp_gain": gain, "xp": xp, "level": level, "level_ups": ups,
                          "lives": lives, "max_lives": max_lives}
        logger.info("Progression %s %s: %s", day_iso, u.slug, result[u.slug])
    store.set_state("progression_settled_through", day_iso)
    return result
