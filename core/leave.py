"""
core/leave.py: система «вакейшенів та ілнесів» (відпустка / лікарняний).
Управління записами leave, нормалізація виду (sick / vacation),
перевірка активності за датами та опис статусу.
"""
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional, Union

from database.store import Store

_SICK_SYNONYMS = {
    "sick", "illness", "ill", "хвороба", "лікарняний", "болен", "захворів",
    "захворіла", "хворіє", "хворію", "больничный", "заболел", "заболела",
}

_VACATION_SYNONYMS = {
    "vacation", "відпустка", "vacay", "відпустці", "отпуск", "отдых", "holiday", "holidays",
}


def normalize_kind(kind: str) -> str:
    raw = (kind or "").strip().lower()
    if raw in _SICK_SYNONYMS:
        return "sick"
    if raw in _VACATION_SYNONYMS:
        return "vacation"
    raise ValueError(f"unknown leave kind: {kind!r}")


def _challenge_today() -> date:
    """Поточний день челенджу (Київ, ролловер 02:30), а не календарна дата сервера."""
    from core import dayroll
    from core.config import TIMEZONE
    return dayroll.challenge_date(datetime.now(TIMEZONE))


def _parse_date(val: Union[date, str, None], default_today: bool = False) -> Optional[date]:
    if val is None or val == "":
        return _challenge_today() if default_today else None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, str):
        return date.fromisoformat(val.strip()[:10])
    raise ValueError(f"invalid date value: {val!r}")


def set_leave(
    store: Store,
    slug: str,
    kind: str,
    start: Union[date, str, None] = None,
    end: Union[date, str, None] = None,
    set_by: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Встановлює відпустку або лікарняний для учасника.
    Ідемпотентно: той самий (slug, kind, start, end) повертає наявний запис.
    При перетині з активною — стара активна обрізається або скасовується.
    """
    user = store.get_user(slug) or store.find_user(slug)
    if not user:
        raise ValueError(f"unknown participant: {slug!r}")
    normalized_slug = user.slug

    norm_kind = normalize_kind(kind)
    start_d = _parse_date(start, default_today=True)
    assert start_d is not None
    end_d = _parse_date(end, default_today=False)

    if end_d is not None and end_d < start_d:
        raise ValueError(f"end_date ({end_d.isoformat()}) cannot be before start_date ({start_d.isoformat()})")

    start_iso = start_d.isoformat()
    end_iso = end_d.isoformat() if end_d is not None else None

    # Ідемпотентність
    existing = store.find_leave(normalized_slug, norm_kind, start_iso, end_iso)
    if existing:
        return existing

    # Обрізка / скасування попередніх перетинних активних записів
    active_leaves = store.list_leaves(slug=normalized_slug, include_cancelled=False)
    cut_end = (start_d - timedelta(days=1)).isoformat()
    for l in active_leaves:
        l_start = l["start_date"]
        l_end = l["end_date"]
        # Якщо запис починається у день start_iso або пізніше
        if l_start >= start_iso:
            store.cancel_leave(l["id"], ended_by=set_by)
        elif l_end is None or l_end >= start_iso:
            # Обрізаємо до (start_new - 1)
            if cut_end < l_start:
                store.cancel_leave(l["id"], ended_by=set_by)
            else:
                store.update_leave_end(l["id"], cut_end, ended_by=set_by)

    leave_id = store.add_leave(
        normalized_slug,
        norm_kind,
        start_iso,
        end_iso,
        set_by=set_by,
        reason=reason,
    )
    rec = store.get_leave(leave_id)
    assert rec is not None
    return rec


def end_leave(
    store: Store,
    slug: str,
    from_day: Union[date, str, None] = None,
    ended_by: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Завершує активну відпустку/лікарняний учасника з from_day (за замовчуванням сьогодні).
    Ставить end_date = from_day - 1 день; якщо end_date < start_date — запис скасовується (cancelled=1).
    """
    user = store.get_user(slug) or store.find_user(slug)
    if not user:
        raise ValueError(f"unknown participant: {slug!r}")
    normalized_slug = user.slug

    from_d = _parse_date(from_day, default_today=True)
    assert from_d is not None
    from_iso = from_d.isoformat()

    # Шукаємо активну на from_iso або будь-яку відкриту/майбутню
    active = store.get_leave_on(normalized_slug, from_iso)
    if not active:
        active_leaves = store.list_leaves(slug=normalized_slug, include_cancelled=False)
        for l in active_leaves:
            if l["end_date"] is None or l["end_date"] >= from_iso:
                active = l
                break

    if not active:
        return {"slug": normalized_slug, "status": "no_active_leave"}

    new_end_d = from_d - timedelta(days=1)
    new_end_iso = new_end_d.isoformat()
    if new_end_iso < active["start_date"]:
        store.cancel_leave(active["id"], ended_by=ended_by)
    else:
        store.update_leave_end(active["id"], new_end_iso, ended_by=ended_by)

    rec = store.get_leave(active["id"])
    return rec or {"slug": normalized_slug, "status": "ended"}


def leave_on(store: Store, slug: str, day: Union[date, str]) -> Optional[Dict[str, Any]]:
    """Повертає активний запис leave на день day або None."""
    if isinstance(day, (date, datetime)):
        day_iso = day.strftime("%Y-%m-%d")
    else:
        day_iso = str(day).strip()[:10]
    return store.get_leave_on(slug, day_iso)


def describe(leave: Optional[Dict[str, Any]]) -> str:
    """Опис статусу українською, наприклад «на лікарняному до 30.10» або «у відпустці»."""
    if not leave:
        return ""
    kind = leave.get("kind")
    kind_text = "на лікарняному" if kind == "sick" else "у відпустці"
    until = leave.get("end_date") or leave.get("until")
    if until:
        try:
            d = date.fromisoformat(str(until)[:10])
            return f"{kind_text} до {d.strftime('%d.%m')}"
        except Exception:
            return f"{kind_text} до {until}"
    return kind_text
