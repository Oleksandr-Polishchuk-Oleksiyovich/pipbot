"""Агент-суддя: чи є в нових повідомленнях звіт (або скасування) виконання норми."""
import re
from typing import Dict, List, Literal

from pydantic import BaseModel

from agents.base import PromptAgent, render_messages
from core.challenge import normalize_participant
from database.store import ChatMessage


class ParticipantReport(BaseModel):
    participant: Literal["lyosha", "sasha"]
    done: bool
    evidence: str


class JudgeVerdict(BaseModel):
    reports: List[ParticipantReport]


_REPORT_MARKERS = ["відео", "відос", "[надіслано", "video", "зробив", "закр", "готово", "відтис", "віджав"]


class ReportJudgeAgent(PromptAgent):
    prompt_key = "report_judge"

    def judge(self, pending: List[ChatMessage], history: List[ChatMessage], done: Dict[str, bool]) -> JudgeVerdict:
        status = ", ".join(f"{p}: {'вже зараховано' if d else 'ще не зараховано'}" for p, d in done.items())
        prompt = (
            f"ПОТОЧНИЙ СТАТУС: {status}\n\n"
            f"ЖУРНАЛ ЧАТУ ЗА СЬОГОДНІ:\n{render_messages(history)}\n\n"
            f"НОВІ ПОВІДОМЛЕННЯ ДЛЯ ОЦІНКИ:\n{render_messages(pending)}"
        )
        return self.llm.json(self.system_prompt, prompt, JudgeVerdict)

    @staticmethod
    def fallback(pending: List[ChatMessage], done: Dict[str, bool]) -> JudgeVerdict:
        """Евристика на випадок недоступного Gemini: звіт зараховується автору повідомлення."""
        reports = []
        for m in pending:
            text = m.text.lower()
            if not (any(k in text for k in _REPORT_MARKERS) or re.search(r"(^|\s)\+(\s|$)", text)):
                continue
            participant = normalize_participant(m.user_name)
            if participant and not done.get(participant):
                reports.append(ParticipantReport(participant=participant, done=True, evidence=m.text[:80]))
        return JudgeVerdict(reports=reports)
