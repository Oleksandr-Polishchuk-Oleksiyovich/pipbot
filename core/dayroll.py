"""
Межа «дня челенджу» (наказ Саші): підсумок дня й перехід на нову дату — не о 00:00, а о DAY_ROLLOVER_HOUR:DAY_ROLLOVER_MINUTE
(за замовчуванням 02:30) за київським часом. Усе, що надіслано між 00:00 та 02:30, належить до ПОПЕРЕДНЬОГО дня челенджу.

Лічимо за настінним (wall-clock) київським часом: від локального часу віднімаємо зсув і беремо календарну дату.
Це не залежить від переходу на літній/зимовий час (у Києві він о 03:00/04:00 — після 02:30; дата береться з локального
настінного часу, без арифметики над UTC-зміщенням).
"""
from datetime import date, datetime, timedelta
from typing import Optional

from core import config


def _offset(hour: Optional[int], minute: Optional[int]) -> timedelta:
    h = config.DAY_ROLLOVER_HOUR if hour is None else hour
    m = config.DAY_ROLLOVER_MINUTE if minute is None else minute
    return timedelta(hours=h, minutes=m)


def challenge_date(now: datetime, hour: Optional[int] = None, minute: Optional[int] = None) -> date:
    """Дата дня челенджу для моменту `now` (tz-aware -> переводиться в Europe/Kyiv; naive вважається київським)."""
    if now.tzinfo is not None:
        now = now.astimezone(config.TIMEZONE)
    return (now.replace(tzinfo=None) - _offset(hour, minute)).date()


def day_start_ts(day: date, hour: Optional[int] = None, minute: Optional[int] = None) -> str:
    """Локальний початок дня челенджу `day` у форматі chat_history.created_at ('YYYY-MM-DD HH:MM:SS')."""
    start = datetime.combine(day, datetime.min.time()) + _offset(hour, minute)
    return start.strftime("%Y-%m-%d %H:%M:%S")


def day_utc_bounds(day: date, hour: Optional[int] = None, minute: Optional[int] = None):
    """[start, end) дня челенджу `day` як UTC-рядки 'YYYY-MM-DD HH:MM:SS' (формат video_checks.created_at = CURRENT_TIMESTAMP)."""
    import pytz

    def _utc(naive: datetime) -> str:
        return config.TIMEZONE.localize(naive).astimezone(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S")

    start = datetime.combine(day, datetime.min.time()) + _offset(hour, minute)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time()) + _offset(hour, minute)
    return _utc(start), _utc(end)
