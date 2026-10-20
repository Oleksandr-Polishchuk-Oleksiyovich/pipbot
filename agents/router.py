"""Агент-диспетчер: адмін чи тренер (чи ігнор). Плюс детермінований захист від підробки адмін-маршруту."""
import logging
import re
from typing import List, Literal, Optional

from pydantic import BaseModel

from agents.base import PromptAgent, render_messages
from core.config import ADMIN_PASSPHRASE_MARKERS
from database.store import ChatMessage

logger = logging.getLogger(__name__)

RouteType = Literal["admin", "admin_quick", "admin_dev", "coach", "ignore"]
AdminTier = Literal["quick", "dev"]

DEV_KEYWORDS = [
    "стиль", "правил", "промпт", "prompt", "coach", "тренер",
    "код", "code", "schema", "схем", "таблиц", "table", "migration", "міграц",
    "нова вправа", "нову вправу", "новий челендж", "сутност", "entity", "feature", "фіч",
]

QUICK_KEYWORDS = [
    "життя", "життів", "lives", "норм", "target", "розклад", "schedule",
    "зарахуй", "прогрес", "progress", "лічильник", "counter", "профіль",
]


class RouteDecision(BaseModel):
    route: RouteType
    admin_tier: Optional[AdminTier] = None
    passphrase_detected: bool
    directive: str
    sender: str
    reason: str

    @property
    def is_admin_dev(self) -> bool:
        return self.route == "admin_dev" or (self.route == "admin" and self.admin_tier == "dev")

    @property
    def is_admin_quick(self) -> bool:
        return self.route == "admin_quick" or (self.route == "admin" and self.admin_tier == "quick")

    @property
    def is_admin(self) -> bool:
        return self.route in ("admin", "admin_quick", "admin_dev")


def has_passphrase(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in ADMIN_PASSPHRASE_MARKERS)


class RouterAgent(PromptAgent):
    prompt_key = "router"

    @staticmethod
    def classify_admin_tier(directive: str) -> AdminTier:
        """
        Класифікує наказ адміна:
        'quick' — дрібні data-tweaks in-bot (lives, targets, schedule, progress, counters);
        'dev'   — код, схема, нові вправи/сутності, промпти, стиль тренера.
        """
        lowered = (directive or "").lower()
        if any(kw in lowered for kw in DEV_KEYWORDS):
            return "dev"
        return "quick"

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
        if decision.is_admin and not any(has_passphrase(m.text) for m in pending):
            logger.warning("Router chose admin without passphrase in text — downgraded to coach (%s)", decision.reason)
            return decision.model_copy(update={"route": "coach", "admin_tier": None, "passphrase_detected": False, "directive": ""})

        # Якщо адмін-маршрут підтверджено — нормалізуємо tier та route
        if decision.is_admin:
            if decision.route == "admin_dev":
                tier: AdminTier = "dev"
            elif decision.route == "admin_quick":
                tier = "quick"
            elif decision.admin_tier in ("quick", "dev"):
                tier = decision.admin_tier
            else:
                tier = RouterAgent.classify_admin_tier(decision.directive)
            return decision.model_copy(update={"admin_tier": tier})

        return decision

    @staticmethod
    def fallback(pending: List[ChatMessage]) -> RouteDecision:
        """Детермінований маршрут, коли Gemini недоступний."""
        for m in pending:
            if has_passphrase(m.text):
                text_low = (m.text or "").lower()
                has_dev = any(kw in text_low for kw in DEV_KEYWORDS)
                has_quick = any(kw in text_low for kw in QUICK_KEYWORDS)

                if has_dev:
                    return RouteDecision(
                        route="admin_dev",
                        admin_tier="dev",
                        passphrase_detected=True,
                        directive=m.text,
                        sender=m.user_name,
                        reason="keyword fallback: dev",
                    )
                elif has_quick:
                    return RouteDecision(
                        route="admin_quick",
                        admin_tier="quick",
                        passphrase_detected=True,
                        directive=m.text,
                        sender=m.user_name,
                        reason="keyword fallback: quick",
                    )
                else:
                    # Чиста кодова фраза (bare passphrase) без явного наказу — маршрутизуємо як admin (quick tier)
                    return RouteDecision(
                        route="admin",
                        admin_tier="quick",
                        passphrase_detected=True,
                        directive=m.text,
                        sender=m.user_name,
                        reason="keyword fallback",
                    )

        return RouteDecision(
            route="coach",
            admin_tier=None,
            passphrase_detected=False,
            directive="",
            sender=pending[-1].user_name if pending else "",
            reason="keyword fallback",
        )
