"""
Тул `report_reps` для LLM: людина називає/виправляє кількість повторів у БУДЬ-ЯКІЙ формі
(цифри, слова, сленг: «двадцять три», «півсотні», «десь 20», «50 там і не пахне, я 23 зробив»).

LLM читає повідомлення, сусідні повідомлення автора та контекст відео і викликає `report_reps(...)` зі структурованими
аргументами. Тул валідує аргументи й кладе claim у sink; застосування (спроба дня / done / video_credit) робить ChatFlow —
`_apply_video_correction`. Regex-парсер (core/claims.py) лишається дешевим фолбеком, якщо LLM недоступний.
"""
import logging
from dataclasses import dataclass
from datetime import date
from typing import Callable, Dict, List, Optional

from agents.base import PromptAgent
from core import challenge
from database.store import Store

logger = logging.getLogger(__name__)

EXERCISE_ENUM = ("pushups", "squats")
MAX_REPS = 1000

_EX_ALIASES = {
    "pushups": "pushups", "pushup": "pushups", "push-up": "pushups", "push-ups": "pushups",
    "віджимання": "pushups", "віджимань": "pushups", "віджиматись": "pushups", "відтискання": "pushups",
    "squats": "squats", "squat": "squats", "присідання": "squats", "присідань": "squats",
}
_EX_NONE = {"", "none", "null", "nil", "unknown", "невідомо", "не названо", "-", "any"}


@dataclass
class RepsClaim:
    user: str                      # slug SUBJECT-а: хто виконав повтори (може відрізнятись від того, хто це написав)
    count: int                     # скільки повторів людина стверджує (рівно це число)
    exercise: Optional[str]        # 'pushups' | 'squats' | None (людина вправу не називала)
    corrects_existing_video: bool  # True — виправляє вже пораховане/зараховане; False — просто називає число
    request_id: Optional[str]      # відео, до якого належить число (якщо відомо)
    confidence: float = 1.0
    reported_by: Optional[str] = None  # slug того, хто написав повідомлення (None — невідомо / сам subject)


def normalize_exercise(value) -> Optional[str]:
    """'pushups'|'squats' або None; ValueError, якщо значення не з enum."""
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in _EX_NONE:
        return None
    if v in _EX_ALIASES:
        return _EX_ALIASES[v]
    raise ValueError(f"exercise must be one of {EXERCISE_ENUM} or empty")


def make_report_reps_tool(
    store: Store,
    sink: List[RepsClaim],
    default_user: Optional[str] = None,
    valid_request_ids: Optional[List[str]] = None,
) -> Callable:
    """Повертає функцію-тул `report_reps` (замикання над sink); її сигнатура і docstring — схема для LLM."""
    valid_ids = set(valid_request_ids or [])

    def report_reps(
        subject_user: str,
        count: int,
        exercise: str = "",
        corrects_existing_video: bool = True,
        request_id: str = "",
        confidence: float = 1.0,
    ) -> str:
        """
        Зафіксувати кількість повторів, яку людина САМА назвала або виправила («я зробив 23», «двадцять три», «півсотні»,
        «а не 50, а 23», «десь 20»), АБО яку учасник назвав про відео ІНШОГО учасника («Льоша зробив 36», «я перерахував — у нього
        36», «записуй Льоші 36»). Слова людей важливіші за підрахунок бота. Викликай один раз на людину й відео.
        НЕ викликай, якщо числа немає, або якщо число належить нормі/часу/вазі/підходам.

        Args:
            subject_user: slug учасника, ЧИЇ це повтори (хто виконав): автор повідомлення, якщо він пише про себе; інакше —
                той, про кого/чиє відео йдеться (за цитатою-відповіддю, іменем у тексті, відкритим питанням бота).
                Порожній рядок = сам автор повідомлення.
            count: ціле число повторів 1..1000, яке стверджує людина (саме те, а не число бота й не заперечене «не 50»).
            exercise: 'pushups' або 'squats', ЛИШЕ якщо людина сама назвала вправу; інакше порожній рядок.
            corrects_existing_video: true, якщо виправляє вже пораховане/зараховане ботом; false — просто називає число до підрахунку.
            request_id: request_id відео зі списку в контексті, до якого належить число; порожній, якщо невідомо.
            confidence: 0..1, наскільки ти впевнений у числі (слова-приблизно, «десь 20» — менше).
        """
        slug = challenge.normalize_participant(str(subject_user), store) if str(subject_user or "").strip() else default_user
        if not slug:
            return "Помилка: невідомий subject_user. Передай slug учасника з контексту."
        if isinstance(count, bool):
            return "Помилка: count має бути цілим числом 1..1000."
        try:
            n = int(count)
        except (TypeError, ValueError):
            return "Помилка: count має бути цілим числом 1..1000."
        if not 1 <= n <= MAX_REPS:
            return f"Помилка: count поза межами 1..{MAX_REPS}."
        try:
            ex = normalize_exercise(exercise)
        except ValueError as e:
            return f"Помилка: {e}."
        rid = str(request_id or "").strip() or None
        if rid and valid_ids and rid not in valid_ids:
            rid = None
        try:
            conf = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            conf = 1.0
        sink.append(RepsClaim(user=slug, count=n, exercise=ex, corrects_existing_video=bool(corrects_existing_video),
                              request_id=rid, confidence=conf, reported_by=default_user))
        logger.info("report_reps tool: user=%s count=%s exercise=%s corrects=%s request_id=%s conf=%.2f",
                    slug, n, ex, bool(corrects_existing_video), rid, conf)
        return f"Прийнято: {slug} — {n} повторів" + (f" ({ex})" if ex else "") + ". Більше нічого викликати не треба."

    return report_reps


def last_claim_for(claims: List[RepsClaim], slug: str) -> Optional[RepsClaim]:
    mine = [c for c in claims if c.user == slug]
    return mine[-1] if mine else None


class RepsClaimAgent(PromptAgent):
    """LLM читає повідомлення людини + контекст і викликає report_reps. Повертає список claim-ів (порожній = числа немає)."""

    prompt_key = "reps_claim"

    @property
    def system_prompt(self) -> str:
        # Редагований промпт з БД + захищене ядро (ПРАВИЛА ДОВІРИ та приклади) в кінці.
        from core import trust_core
        return trust_core.ensure_core(super().system_prompt)

    def extract(
        self,
        today: date,
        author_slug: str,
        author_name: str,
        mode: str,
        target_text: str,
        context_text: str,
        videos_block: str,
        request_ids: List[str],
        participants_block: str = "",
        extra_context: str = "",
    ) -> List[RepsClaim]:
        """
        mode: 'dispute' (повідомлення після відео — виправлення/уточнення) або 'video' (підпис і сусідні тексти до нового відео).
        Кидає LLMUnavailable, якщо LLM недоступний і жодного тул-виклику не було.
        """
        from core.llm import LLMUnavailable
        sink: List[RepsClaim] = []
        tool = make_report_reps_tool(self.store, sink, default_user=author_slug, valid_request_ids=request_ids)
        where = (
            "Повідомлення людини ПІСЛЯ відео (вона може виправляти підрахунок бота)."
            if mode == "dispute" else
            "Підпис до НОВОГО відео та повідомлення автора поруч із ним (число, яке людина заявляє за це відео)."
        )
        prompt = (
            f"Дата челенджу: {today.isoformat()}. Автор: {author_name} (slug='{author_slug}').\n"
            f"{where}\n\n"
            + (f"УЧАСНИКИ ЧЕЛЕНДЖУ:\n{participants_block}\n\n" if participants_block else "") +
            f"ВІДЕО ЗА СЬОГОДНІ (owner — чиє відео; request_id, що порахував бот, що зараховано):\n{videos_block or '(немає)'}\n\n"
            + (f"{extra_context}\n" if extra_context else "") +
            f"НЕДАВНІ ПОВІДОМЛЕННЯ В ЧАТІ (контекст):\n{context_text or '(немає)'}\n\n"
            f"ТЕКСТ ДЛЯ АНАЛІЗУ (від {author_name}):\n{target_text}\n"
        )
        try:
            self.llm.text(self.system_prompt, prompt, tools=[tool], temperature=0.0)
        except LLMUnavailable:
            if not sink:   # модель могла викликати тул і потім відповісти порожнім текстом — claim уже є
                raise
        return sink
