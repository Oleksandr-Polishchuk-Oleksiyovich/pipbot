"""Агент-суддя: чи є в нових повідомленнях звіт (або скасування) виконання норми для будь-якого активного учасника."""
import re
from typing import Dict, List, Optional

from pydantic import BaseModel

from agents.base import PromptAgent, render_messages
from core.challenge import normalize_participant
from database.store import ChatMessage, Store


class ParticipantReport(BaseModel):
    participant: str
    done: bool
    evidence: str


class JudgeVerdict(BaseModel):
    reports: List[ParticipantReport]


_REPORT_MARKERS = [
    "відео", "відос", "[надіслано", "video", "зробив", "зробила",
    "закр", "готово", "відтис", "віджав", "присів", "присіла",
]


class ReportJudgeAgent(PromptAgent):
    prompt_key = "report_judge"

    def judge(self, pending: List[ChatMessage], history: List[ChatMessage], done: Dict[str, bool]) -> JudgeVerdict:
        active_users = self.store.list_users(status="active")
        participants_lines = []
        for u in active_users:
            d = done.get(u.slug, False)
            aliases_str = ", ".join(u.aliases[:8]) if u.aliases else u.telegram_name
            participants_lines.append(
                f"- slug='{u.slug}': {u.display_name} (у чаті: '{u.telegram_name}', псевдоніми: {aliases_str}) -> "
                f"{'вже зараховано' if d else 'ще не зараховано'}"
            )
        status_block = "\n".join(participants_lines) or ", ".join(
            f"{p}: {'вже зараховано' if d else 'ще не зараховано'}" for p, d in done.items()
        )
        prompt = (
            f"УЧАСНИКИ ТА ПОТОЧНИЙ СТАТУС:\n{status_block}\n\n"
            f"ЖУРНАЛ ЧАТУ ЗА СЬОГОДНІ:\n{render_messages(history)}\n\n"
            f"НОВІ ПОВІДОМЛЕННЯ ДЛЯ ОЦІНКИ:\n{render_messages(pending)}"
        )
        raw_verdict = self.llm.json(self.system_prompt, prompt, JudgeVerdict)
        normalized_reports = []
        for r in raw_verdict.reports:
            slug = normalize_participant(r.participant, self.store) or r.participant
            if slug in done:
                if r.done:
                    author_msgs = [m for m in pending if normalize_participant(m.user_name, self.store) == slug]
                    if author_msgs and all(
                        "не є відеозвітом" in m.text.lower() or "gif/анімаці" in m.text.lower()
                        for m in author_msgs
                    ):
                        continue
                normalized_reports.append(ParticipantReport(participant=slug, done=r.done, evidence=r.evidence))
        return JudgeVerdict(reports=normalized_reports)

    @staticmethod
    def fallback(pending: List[ChatMessage], done: Dict[str, bool], store: Optional[Store] = None) -> JudgeVerdict:
        """Евристика на випадок недоступного Gemini: звіт зараховується автору повідомлення."""
        reports = []
        for m in pending:
            text = m.text.lower()
            if "не є відеозвітом" in text or "gif/анімаці" in text or "анімація/gif" in text:
                continue
            if not (any(k in text for k in _REPORT_MARKERS) or re.search(r"(^|\s)\+(\s|$)", text)):
                continue
            participant = normalize_participant(m.user_name, store)
            if participant and not done.get(participant):
                reports.append(ParticipantReport(participant=participant, done=True, evidence=m.text[:80]))
        return JudgeVerdict(reports=reports)
