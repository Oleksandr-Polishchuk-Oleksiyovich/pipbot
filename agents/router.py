"""Агент-диспетчер: адмін чи тренер (чи ігнор). Плюс детермінований захист від підробки адмін-маршруту."""
import logging
from typing import List, Literal

from pydantic import BaseModel

from agents.base import PromptAgent, render_messages
from core.config import ADMIN_PASSPHRASE_MARKERS
from database.store import ChatMessage

logger = logging.getLogger(__name__)


class RouteDecision(BaseModel):
    route: Literal["admin", "coach", "ignore"]
    passphrase_detected: bool
    directive: str
    sender: str
    reason: str


def has_passphrase(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in ADMIN_PASSPHRASE_MARKERS)


class RouterAgent(PromptAgent):
    prompt_key = "router"

    def decide(self, pending: List[ChatMessage], history: List[ChatMessage]) -> RouteDecision:
        prompt = (
            f"ЖУРНАЛ ЧАТУ ЗА СЬОГОДНІ (контекст):\n{render_messages(history)}\n\n"
            f"НОВІ ПОВІДОМЛЕННЯ, ЯКІ ТРЕБА МАРШРУТИЗУВАТИ:\n{render_messages(pending)}"
        )
        decision = self.llm.json(self.system_prompt, prompt, RouteDecision)
        return self._guard(decision, pending)

    @staticmethod
    def _guard(decision: RouteDecision, pending: List[ChatMessage]) -> RouteDecision:
        # Адмін-права даються лише за реальної кодової фрази в тексті — модель не може їх «нафантазувати»
        # чи бути переконаною промпт-ін'єкцією.
        if decision.route == "admin" and not any(has_passphrase(m.text) for m in pending):
            logger.warning("Router chose admin without passphrase in text — downgraded to coach (%s)", decision.reason)
            return decision.model_copy(update={"route": "coach", "passphrase_detected": False, "directive": ""})
        return decision

    @staticmethod
    def fallback(pending: List[ChatMessage]) -> RouteDecision:
        """Детермінований маршрут, коли Gemini недоступний."""
        for m in pending:
            if has_passphrase(m.text):
                return RouteDecision(route="admin", passphrase_detected=True, directive=m.text,
                                     sender=m.user_name, reason="keyword fallback")
        return RouteDecision(route="coach", passphrase_detected=False, directive="",
                             sender=pending[-1].user_name if pending else "", reason="keyword fallback")
