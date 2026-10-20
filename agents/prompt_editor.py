"""
Редагування динамічного промпту тренера трьома вузькими агентами:
  PromptConflictAgent (рішення: append / override / replace_all / redundant)
    → PromptMergerAgent (текст)
    → PromptVerifierAgent (рішення: прийняти чи переробити; одна повторна спроба з зауваженнями).
"""
import logging
from dataclasses import dataclass
from typing import List, Literal, Optional

from pydantic import BaseModel

from agents.base import PromptAgent

logger = logging.getLogger(__name__)

COACH_DYNAMIC_KEY = "coach_dynamic"
MAX_DYNAMIC_PROMPT_CHARS = 6000


@dataclass(frozen=True)
class RuleChange:
    rule: str          # нормалізоване формулювання правила від адмін-агента
    quote: str         # дослівна цитата наказу — джерело істини для назв, титулів і чисел
    until: str         # "" — постійно, або YYYY-MM-DD — діє до цієї дати включно
    author: str
    issued: str        # дата наказу, YYYY-MM-DD

    def render(self) -> str:
        scope = f"діє до {self.until} включно" if self.until else "постійне"
        return (
            f"Правило: {self.rule}\n"
            f"Дослівний наказ: «{self.quote}»\n"
            f"Термін: {scope}\n"
            f"Автор і дата: {self.author}, {self.issued}"
        )


class ConflictAnalysis(BaseModel):
    strategy: Literal["append", "override", "replace_all", "redundant"]
    conflicts: List[str]
    new_rules: List[str]
    reason: str


class MergeReview(BaseModel):
    approved: bool
    new_rule_covered: bool
    lost_rules: List[str]
    invented_rules: List[str]
    distorted_details: List[str]
    feedback: str


class PromptConflictAgent(PromptAgent):
    prompt_key = "prompt_conflict"

    def analyze(self, current: str, change: RuleChange) -> ConflictAnalysis:
        prompt = (
            f"ЧИННІ ДИНАМІЧНІ ПРАВИЛА ТРЕНЕРА:\n---\n{current or '(порожньо)'}\n---\n\n"
            f"НОВЕ ПРАВИЛО ВІД АДМІНА:\n{change.render()}"
        )
        return self.llm.json(self.system_prompt, prompt, ConflictAnalysis)


class PromptMergerAgent(PromptAgent):
    prompt_key = "prompt_merger"

    def merge(self, current: str, change: RuleChange, analysis: ConflictAnalysis,
              review_feedback: Optional[str] = None) -> str:
        prompt = (
            f"ЧИННІ ПРАВИЛА:\n---\n{current or '(порожньо)'}\n---\n\n"
            f"НОВЕ ПРАВИЛО:\n{change.render()}\n\n"
            f"РЕКОМЕНДОВАНА СТРАТЕГІЯ: {analysis.strategy}\n"
            f"Суперечності: {analysis.conflicts or 'немає'}\n"
            f"Що нового: {analysis.new_rules or 'немає'}"
        )
        if review_feedback:
            prompt += f"\n\nПОПЕРЕДНЯ СПРОБА ВІДХИЛЕНА РЕЦЕНЗЕНТОМ. ВИПРАВ:\n{review_feedback}"
        text = self.llm.text(self.system_prompt, prompt, temperature=0.2).strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("markdown").strip()
        return text


class PromptVerifierAgent(PromptAgent):
    prompt_key = "prompt_verifier"

    def review(self, before: str, change: RuleChange, analysis: ConflictAnalysis, after: str) -> MergeReview:
        prompt = (
            f"ПРАВИЛА ДО ЗМІНИ:\n---\n{before or '(порожньо)'}\n---\n\n"
            f"НОВЕ ПРАВИЛО:\n{change.render()}\n\n"
            f"СТРАТЕГІЯ: {analysis.strategy}; суперечності, які мали бути замінені: {analysis.conflicts or 'немає'}\n\n"
            f"ПРАВИЛА ПІСЛЯ ЗМІНИ:\n---\n{after}\n---"
        )
        return self.llm.json(self.system_prompt, prompt, MergeReview)


@dataclass
class PromptEditResult:
    changed: bool
    strategy: str
    version: int
    content: str
    reason: str
    review: Optional[MergeReview] = None


class CoachPromptEditor:
    def __init__(self, conflict: PromptConflictAgent, merger: PromptMergerAgent, verifier: PromptVerifierAgent):
        self.conflict = conflict
        self.merger = merger
        self.verifier = verifier
        self.store = conflict.store

    def apply(self, change: RuleChange, updated_by: Optional[str] = None) -> PromptEditResult:
        current = self.store.get_prompt(COACH_DYNAMIC_KEY) or ""
        analysis = self.conflict.analyze(current, change)
        logger.info("Prompt conflict analysis: %s", analysis.model_dump())
        version = self.store.get_prompt_version(COACH_DYNAMIC_KEY)
        if analysis.strategy == "redundant":
            return PromptEditResult(False, analysis.strategy, version, current, analysis.reason)

        base = "" if analysis.strategy == "replace_all" else current
        merged, review, feedback = "", None, None
        for _ in range(2):
            merged = self.merger.merge(base, change, analysis, feedback)
            if not merged or len(merged) > MAX_DYNAMIC_PROMPT_CHARS:
                feedback = f"Текст має бути непорожнім і коротшим за {MAX_DYNAMIC_PROMPT_CHARS} символів."
                continue
            review = self.verifier.review(base, change, analysis, merged)
            logger.info("Prompt merge review: %s", review.model_dump())
            if review.approved:
                break
            feedback = review.feedback
        else:
            # Обидві спроби відхилені — краще зберегти найкращу, ніж загубити наказ адміна; лог фіксує зауваження.
            logger.warning("Saving merged prompt despite review rejection: %s", feedback)
        if not merged or len(merged) > MAX_DYNAMIC_PROMPT_CHARS:
            raise ValueError(f"Merged prompt rejected (length={len(merged)})")
        updater = updated_by or change.author
        version = self.store.save_prompt(COACH_DYNAMIC_KEY, merged, updater)
        return PromptEditResult(True, analysis.strategy, version, merged, analysis.reason, review)

    def reset(self, author: str, updated_by: Optional[str] = None) -> int:
        return self.store.save_prompt(COACH_DYNAMIC_KEY, "", updated_by or author)
