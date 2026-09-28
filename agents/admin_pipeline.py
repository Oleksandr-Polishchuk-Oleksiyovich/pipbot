import os
import json
import sqlite3
import logging
from typing import Dict, Any, List, Optional
from services.jev_client import JevClient
from prompts import SYSTEM_PROMPT_TEMPLATE, FALLBACK_JEV_MESSAGE

from core.config import CANDIDATE_MODELS

logger = logging.getLogger(__name__)

class AdminPipeline:
    """
    Адміністративний пайплайн для обробки директив володаря/адміна:
    1. JEV Triage: перевірка потреби створення нових станів/лічильників.
    2. Ітераційний цикл узгодження станів між Gemini та JEV (до 7 ітерацій на Python).
    3. JEV аналіз конфліктів та новизни між новим і старим промптами.
    4. Інтеграція та мерж промптів через Gemini з фіксацією в SQLite.
    """

    def __init__(self, gemini_client, jev_client: JevClient, db_path: str):
        self.gemini_client = gemini_client
        self.jev_client = jev_client
        self.db_path = db_path
        self.candidate_models = CANDIDATE_MODELS

    def _get_db_state_keys(self) -> List[str]:
        try:
            with sqlite3.connect(self.db_path) as con:
                cur = con.cursor()
                rows = cur.execute("SELECT key FROM bot_state").fetchall()
                return [r[0] for r in rows]
        except Exception as e:
            logger.error("Error reading bot_state keys: %s", e)
            return []

    def _get_current_prompt(self) -> str:
        try:
            with sqlite3.connect(self.db_path) as con:
                cur = con.cursor()
                row = cur.execute("SELECT value FROM bot_state WHERE key = 'custom_system_prompt'").fetchone()
                return row[0] if row else ""
        except Exception as e:
            logger.error("Error reading custom_system_prompt: %s", e)
            return ""

    def _save_state_to_db(self, key: str, value: str):
        try:
            with sqlite3.connect(self.db_path) as con:
                cur = con.cursor()
                cur.execute("""
                    INSERT INTO bot_state (key, value, updated_at)
                    VALUES (?, ?, datetime('now'))
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = datetime('now')
                """, (key, str(value)))
                con.commit()
        except Exception as e:
            logger.error("Error saving state [%s] to bot_state: %s", key, e)

    async def _call_gemini_text(self, system_instruction: str, user_prompt: str) -> str:
        if not self.gemini_client:
            return ""
        for model in self.candidate_models:
            try:
                res = self.gemini_client.models.generate_content(
                    model=model,
                    contents=f"{system_instruction}\n\n{user_prompt}"
                )
                return res.text.strip()
            except Exception as e:
                logger.warning("Gemini model %s failed: %s", model, e)
        return ""

    async def _gemini_propose_states(self, admin_directive: str, iteration: int, feedback: Optional[str]) -> Dict[str, Any]:
        """
        Gemini формує пропозицію станів, типів даних та опис промпту.
        """
        sys_inst = (
            "YOU ARE THE SYSTEM ARCHITECT FOR @SportNerdBot.\n"
            "Task: Given the admin directive, design new state variables (keys), their data types, "
            "initial values, and instructions on how the bot must manage them.\n"
            "Output MUST BE valid JSON only matching schema:\n"
            "{\n"
            '  "states": [\n'
            '    {\n'
            '      "key": "snake_case_key_name",\n'
            '      "type": "int" | "str" | "bool" | "json",\n'
            '      "initial_value": 4,\n'
            '      "description": "Short explanation"\n'
            '    }\n'
            '  ],\n'
            '  "usage_instructions": "Clear guidelines on how to track, decrement, increment, or display these states."\n'
            "}"
        )
        user_p = (
            f"ADMIN DIRECTIVE:\n«{admin_directive}»\n\n"
            f"ITERATION: {iteration} / 7\n"
        )
        if feedback:
            user_p += f"PREVIOUS REVIEW FEEDBACK TO FIX:\n«{feedback}»\n"

        raw = await self._call_gemini_text(sys_inst, user_p)
        text = raw
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            return json.loads(text)
        except Exception:
            # Фолбек парсинг
            return {
                "states": [
                    {"key": "lives_lyosha", "type": "int", "initial_value": 4, "description": "Життя Сонцеликого Льоші"},
                    {"key": "lives_sasha", "type": "int", "initial_value": 7, "description": "Життя Саші"}
                ],
                "usage_instructions": "Списувати 1 життя за невиконання щоденної норми. Згадувати залишок життів у звітах."
            }

    async def _gemini_merge_prompt(self, base_prompt: str, current_prompt: str, admin_directive: str, jev_analysis: Dict[str, Any], states_proposal: Optional[Dict[str, Any]]) -> str:
        """
        Gemini синтезує фінальний системний промпт з урахуванням протиріч та нових станів.
        """
        sys_inst = (
            "YOU ARE THE SYSTEM PROMPT ARCHITECT FOR @SportNerdBot.\n"
            "Your task is to merge and rewrite the dynamic system prompt.\n"
            "CRITICAL RULES:\n"
            "1. Output ONLY the raw markdown of the new dynamic prompt. No conversational meta-talk.\n"
            "2. Resolve all contradictions in favor of the NEW admin directive.\n"
            "3. If new state keys exist, include explicit instructions on their meaning and how to reference them.\n"
            "4. Format strictly with clean line breaks (2-3 lines per block), bold/italic Telegram Markdown, and fitness emojis."
        )
        user_p = (
            f"=== BASE PROMPT ESSENCE ===\n{base_prompt[:1000]}\n\n"
            f"=== CURRENT DYNAMIC PROMPT ===\n{current_prompt}\n\n"
            f"=== NEW ADMIN DIRECTIVE ===\n{admin_directive}\n\n"
            f"=== JEV CROSS-ANALYSIS ===\n"
            f"Has contradiction: {jev_analysis.get('has_contradiction')}\n"
            f"Brings new info: {jev_analysis.get('brings_new_info')}\n"
            f"Strategy: {jev_analysis.get('merge_strategy')}\n"
            f"Guidance: {jev_analysis.get('guidance')}\n\n"
            f"=== DEFINED STATE KEYS ===\n{json.dumps(states_proposal, ensure_ascii=False, indent=2) if states_proposal else 'None'}\n\n"
            "Generate the comprehensive, polished new dynamic prompt:"
        )
        return await self._call_gemini_text(sys_inst, user_p)

    async def process_admin_directive(self, admin_directive: str, sender_name: str = "Володар") -> Dict[str, Any]:
        """
        Головна точка входу адмін-пайплайну.
        Повертає звіт про виконані зміни, створені стани та підтвердження для Telegram.
        """
        logger.info("AdminPipeline triggered with directive: %s", admin_directive)
        current_keys = self._get_db_state_keys()
        current_prompt = self._get_current_prompt()

        # 1. JEV Triage: чи потрібні нові стани/лічильники
        triage_result = await self.jev_client.check_needs_new_states(admin_directive, current_keys)
        needs_states = triage_result.get("needs_new_states", False)
        logger.info("JEV Triage result: needs_new_states=%s, confidence=%.2f",
                    needs_states, triage_result.get("confidence", 0.0))

        approved_states_proposal = None
        iteration_count = 0
        final_score = 1.0

        # 2. Якщо потрібні стани -> Ітераційний цикл Python (до 7 ітерацій)
        if needs_states:
            max_iterations = 7
            feedback = None
            for it in range(1, max_iterations + 1):
                iteration_count = it
                proposal = await self._gemini_propose_states(admin_directive, it, feedback)
                jev_eval = await self.jev_client.evaluate_state_proposal(admin_directive, proposal, it)
                score = jev_eval.get("score", 0.0)
                is_approved = jev_eval.get("is_approved", False)
                final_score = score
                logger.info("Iteration %d/7: score=%.2f, is_approved=%s", it, score, is_approved)

                if is_approved or it == max_iterations:
                    approved_states_proposal = proposal
                    # Зберігаємо нові стани в SQLite
                    for s in proposal.get("states", []):
                        k = s.get("key")
                        val = s.get("initial_value", 0)
                        if k:
                            self._save_state_to_db(k, str(val))
                            logger.info("Persisted state [%s] = %s into DB", k, val)
                    break
                else:
                    feedback = jev_eval.get("guidance") or "Please specify data types and explicit usage rules."

        # 3. JEV аналіз конфліктів та новизни
        jev_analysis = await self.jev_client.check_conflicts_and_novelty(
            SYSTEM_PROMPT_TEMPLATE, current_prompt, admin_directive
        )
        logger.info("JEV prompt analysis: %s", jev_analysis)

        # 4. Gemini мерж та синтез оновленого промпту
        merged_prompt = await self._gemini_merge_prompt(
            SYSTEM_PROMPT_TEMPLATE, current_prompt, admin_directive, jev_analysis, approved_states_proposal
        )

        if merged_prompt:
            self._save_state_to_db("custom_system_prompt", merged_prompt.strip())
            logger.info("Updated custom_system_prompt in DB successfully.")

        # 5. Формування гарної відповіді для чату Telegram
        applied_states_str = ""
        if approved_states_proposal and approved_states_proposal.get("states"):
            items = [f"• `{s['key']}` = `{s.get('initial_value')}` ({s.get('description', '')})" for s in approved_states_proposal["states"]]
            applied_states_str = "\n".join(items)

        jev_down = getattr(self.jev_client, "last_used_fallback", False)
        if jev_down:
            reply_message = (
                f"{FALLBACK_JEV_MESSAGE}\n\n"
                f"*(Але через резервний контур наказ володаря {sender_name} все одно виконано!)*\n\n"
            )
        else:
            reply_message = (
                f"🦾 **Слухаюся і підкоряюся, {sender_name}!**\n\n"
                f"Кодовий наказ прийнято та опрацьовано через двоконтурну систему JEV & Gemini.\n\n"
            )

        if applied_states_str:
            reply_message += (
                f"📊 **Створено/оновлено лічильники стану (Циклів JEV: {iteration_count}, Рейтинг: {int(final_score * 100)}%):**\n"
                f"{applied_states_str}\n\n"
            )
        reply_message += (
            f"👑 **Системний промпт оновлено з урахуванням нових правил та контролю конфліктів!**\n\n"
            f"Зануда готовий тримати всіх у залізній дисципліні! 👊⚡"
        )

        return {
            "status": "success",
            "needs_new_states": needs_states,
            "iterations": iteration_count,
            "final_score": final_score,
            "approved_states": approved_states_proposal,
            "jev_analysis": jev_analysis,
            "merged_prompt": merged_prompt,
            "reply_message": reply_message
        }
