import os
import json
import logging
import httpx
from typing import Dict, Any, Tuple, Optional

logger = logging.getLogger(__name__)

class JevClient:
    """
    Асинхронний клієнт для взаємодії з JEV API (https://www.jevai.org).
    Забезпечує System-1 швидку диспетчеризацію, валідацію рішень,
    оцінку пропозицій нових станів та аналіз конфліктів у промптах.
    Містить стійкий фолбек у разі недоступності зовнішнього сервера.
    """

    def __init__(self, api_key: Optional[str] = None, base_url: str = "https://www.jevai.org", timeout: float = 10.0, gemini_client = None):
        self.api_key = api_key or os.getenv("JEV_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.gemini_client = gemini_client
        self.last_used_fallback = False

    async def call_decisions(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Any]:
        """
        Базовий виклик до /api/v1/decisions JEV API.
        """
        url = f"{self.base_url}/api/v1/decisions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "SportNerdBot-JevClient/1.0"
        }
        payload = {
            "state": state,
            "questions": questions
        }

        if self.api_key:
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(url, headers=headers, json=payload)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("code") == 0:
                            self.last_used_fallback = False
                            return data.get("data", {})
                    logger.warning("JEV API returned status %s: %s", resp.status_code, resp.text[:200])
            except Exception as e:
                logger.warning("JEV API request failed (%s): %s", url, e)

        # Фолбек на AI або детерміновану евристику
        self.last_used_fallback = True
        return await self._fallback_decide(state, questions)

    async def _fallback_decide(self, state: Dict[str, Any], questions: Dict[str, Any]) -> Dict[str, Any]:
        """
        Фолбек-оцінювач при недоступності JEV сервера (наприклад, 502 Bad Gateway або відсутності ключа).
        Використовує Gemini зі строгою системною роллю JEV Evaluator.
        """
        if self.gemini_client:
            try:
                prompt = (
                    "YOU ARE THE JEV DECISION ENGINE (System-1 Decision Evaluator).\n"
                    "Evaluate the provided state against the questions and return ONLY a valid JSON object matching this schema:\n"
                    "{\n"
                    '  "decision": "string",\n'
                    '  "confidence": float (0.0 to 1.0),\n'
                    '  "guidance": "string",\n'
                    '  "answers": {\n'
                    '     "<question_key>": {\n'
                    '         "choice": "string",\n'
                    '         "confidence": float (0.0 to 1.0),\n'
                    '         "noul": float (0.0 to 1.0, probability of yes),\n'
                    '         "score": float (0.0 to 1.0)\n'
                    '     }\n'
                    "  }\n"
                    "}\n\n"
                    f"STATE:\n{json.dumps(state, ensure_ascii=False, indent=2)}\n\n"
                    f"QUESTIONS:\n{json.dumps(questions, ensure_ascii=False, indent=2)}"
                )
                
                from core.config import CANDIDATE_MODELS
                for model in CANDIDATE_MODELS:
                    try:
                        res = self.gemini_client.models.generate_content(
                            model=model,
                            contents=prompt
                        )
                        text = res.text.strip()
                        if text.startswith("```"):
                            text = text.split("```")[1]
                            if text.startswith("json"):
                                text = text[4:]
                            text = text.strip()
                        return json.loads(text)
                    except Exception as err:
                        logger.warning("Gemini fallback model %s failed: %s", model, err)
            except Exception as e:
                logger.error("JEV Gemini fallback error: %s", e)

        # Евристичний дефолт
        answers = {}
        for q_key, q_val in questions.items():
            answers[q_key] = {
                "noul": 0.95,
                "score": 0.95,
                "confidence": 0.95,
                "choice": "allow"
            }
        return {
            "decision": "allow",
            "confidence": 0.95,
            "guidance": "Heuristic fallback evaluation",
            "answers": answers
        }

    async def check_needs_new_states(self, admin_prompt: str, current_keys: list) -> Dict[str, Any]:
        """
        Перевіряє, чи вимагає наказ адміна створення нових станів, ключів або лічильників.
        """
        state = {
            "admin_prompt": admin_prompt,
            "current_existing_state_keys": current_keys
        }
        questions = {
            "needs_new_states": {
                "type": "noul",
                "instructions": (
                    "Does this admin directive require tracking NEW state keys, variables, quotas, or counters "
                    "(such as life counters, points, inventory, custom targets) that do not already exist in the current keys?"
                )
            },
            "reasoning": {
                "type": "choice",
                "instructions": "State the classification reason.",
                "criteria": {
                    "new_counter_or_state": "The directive introduces new metrics/counters to track over time (e.g. lives, points).",
                    "style_or_behavior_only": "The directive only modifies tone, nickname, style, or wording without tracking metrics.",
                    "existing_state_mutation": "The directive only modifies values of already existing keys."
                }
            }
        }

        res = await self.call_decisions(state, questions)
        answers = res.get("answers", {})
        noul_val = answers.get("needs_new_states", {}).get("noul", 0.0)
        confidence = answers.get("needs_new_states", {}).get("confidence", 0.8)
        choice = answers.get("reasoning", {}).get("choice", "style_or_behavior_only")

        # Якщо noul > 0.5 або choice вказує на лічильники
        needs_states = (noul_val >= 0.5) or (choice == "new_counter_or_state")
        return {
            "needs_new_states": needs_states,
            "confidence": confidence,
            "reasoning": choice,
            "guidance": res.get("guidance", "")
        }

    async def evaluate_state_proposal(self, admin_prompt: str, proposal: Dict[str, Any], iteration: int) -> Dict[str, Any]:
        """
        Оцінює пропозицію станів від Gemini (рейтинг від 0.0 до 1.0).
        Поріг прийняття: >= 0.90 (90%).
        """
        state = {
            "admin_prompt": admin_prompt,
            "iteration": iteration,
            "proposed_states": proposal.get("states", []),
            "proposed_usage_instructions": proposal.get("usage_instructions", "")
        }
        questions = {
            "approval_score": {
                "type": "score",
                "instructions": (
                    "Score how accurately and cleanly this proposal captures all needed states, "
                    "variable names, default values, and operational rules from the admin directive."
                ),
                "criteria": [
                    "Inadequate: Missing critical states or unclear definitions",
                    "Needs Improvement: Partial coverage with ambiguous usage rules",
                    "Acceptable: Mostly complete but missing some edge cases (below 90%)",
                    "Approved: Fully complete, accurate and high quality specification (90%+)"
                ]
            },
            "is_complete": {
                "type": "noul",
                "instructions": "Is this specification completely ready to be applied into production DB?"
            }
        }

        res = await self.call_decisions(state, questions)
        answers = res.get("answers", {})
        
        score_data = answers.get("approval_score", {})
        raw_score = score_data.get("score")
        if raw_score is None:
            raw_score = answers.get("is_complete", {}).get("noul", 0.9)
        try:
            score = float(raw_score)
            if score > 1.0: # якщо повертає 0..100
                score = score / 100.0
        except Exception:
            score = 0.95

        guidance = res.get("guidance") or score_data.get("guidance") or ""
        return {
            "score": score,
            "is_approved": score >= 0.90,
            "guidance": guidance,
            "raw_response": res
        }

    async def check_conflicts_and_novelty(self, base_prompt: str, current_prompt: str, new_directive: str) -> Dict[str, Any]:
        """
        Аналізує чи є в новій частині промпту протиріччя зі старим і яку нову інформацію вона несе.
        """
        state = {
            "base_system_prompt": base_prompt[:1500],
            "current_dynamic_prompt": current_prompt[:1500],
            "new_admin_directive": new_directive
        }
        questions = {
            "has_contradiction": {
                "type": "noul",
                "instructions": "Are there direct contradictions or conflicting instructions between the new directive and existing prompt rules?"
            },
            "brings_new_info": {
                "type": "noul",
                "instructions": "Does the new directive introduce genuinely new rules, titles, constraints, or states not present in existing prompt?"
            },
            "merge_strategy": {
                "type": "choice",
                "instructions": "What merge strategy should Gemini take?",
                "criteria": {
                    "override_conflicts_and_add_new": "Override conflicting old rules with the new directive and append new state mechanics.",
                    "pure_addition": "No contradictions found, simply add new rules and instructions.",
                    "redundant": "The directive is already covered by existing prompt."
                }
            }
        }

        res = await self.call_decisions(state, questions)
        answers = res.get("answers", {})
        has_contra = answers.get("has_contradiction", {}).get("noul", 0.0) >= 0.5
        brings_new = answers.get("brings_new_info", {}).get("noul", 1.0) >= 0.5
        strategy = answers.get("merge_strategy", {}).get("choice", "override_conflicts_and_add_new")

        return {
            "has_contradiction": has_contra,
            "brings_new_info": brings_new,
            "merge_strategy": strategy,
            "guidance": res.get("guidance", "")
        }
