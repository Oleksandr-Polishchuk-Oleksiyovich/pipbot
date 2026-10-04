"""
Тонка синхронна обгортка над Gemini з перебором моделей.
Агенти викликають її через asyncio.to_thread, тож Python-тули (AFC) можуть синхронно ходити в SQLite.
"""
import json
import logging
from typing import Callable, List, Optional, Sequence, Type, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    """Жодна з моделей не дала валідної відповіді."""


class GeminiLLM:
    def __init__(self, client: genai.Client, models: Sequence[str]):
        self._client = client
        self._models: List[str] = list(models)
        self.last_model: Optional[str] = None

    @classmethod
    def from_api_key(cls, api_key: str, models: Sequence[str], timeout_ms: int = 60_000) -> "GeminiLLM":
        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_ms))
        return cls(client, models)

    def _promote(self, model: str) -> None:
        # Робоча модель стає першою, щоб не витрачати час на ті, що падають.
        if self._models and self._models[0] != model:
            self._models.remove(model)
            self._models.insert(0, model)

    def _run(self, contents: str, config: types.GenerateContentConfig, accept: Callable) -> object:
        errors = []
        for model in list(self._models):
            try:
                resp = self._client.models.generate_content(model=model, contents=contents, config=config)
                result = accept(resp)
                self._promote(model)
                self.last_model = model
                return result
            except Exception as e:  # мережа, 4xx/5xx, невалідний JSON — пробуємо наступну модель
                logger.warning("Gemini model %s failed: %s", model, str(e)[:300])
                errors.append(f"{model}: {str(e)[:120]}")
        raise LLMUnavailable("; ".join(errors[-3:]) or "no models configured")

    def text(self, system: str, prompt: str, *, tools: Optional[list] = None, temperature: float = 0.7) -> str:
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            tools=tools or None,
            # Без тулів AFC не потрібен (і google-genai інакше пише попередження в лог).
            automatic_function_calling=None if tools else types.AutomaticFunctionCallingConfig(disable=True),
        )

        def accept(resp) -> str:
            text = (resp.text or "").strip()
            if not text:
                raise ValueError("empty response")
            return text

        return self._run(prompt, config, accept)

    def json(self, system: str, prompt: str, schema: Type[T], *, temperature: float = 0.1) -> T:
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            response_mime_type="application/json",
            response_schema=schema,
        )

        def accept(resp) -> T:
            if isinstance(getattr(resp, "parsed", None), schema):
                return resp.parsed
            try:
                return schema.model_validate_json(resp.text or "")
            except ValidationError:
                return schema.model_validate(json.loads(_strip_fences(resp.text or "")))

        return self._run(prompt, config, accept)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()
