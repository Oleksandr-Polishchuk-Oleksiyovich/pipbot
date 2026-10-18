"""
Тонка синхронна обгортка над Gemini з перебором моделей та адаптивним rate-limiter'ом
на циклічному буфері з 17 елементів (під ліміт ~15 RPM).
Агенти викликають її через asyncio.to_thread, тож Python-тули (AFC) можуть синхронно ходити в SQLite.
"""
import json
import logging
import threading
import time
from typing import Callable, List, Optional, Sequence, Type, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

RING_BUFFER_SIZE = 17
WINDOW_MS = 60_000
STEP_DELAY_MS = 650  # 0.65 сек на кожен виклик за останню хвилину


class LLMUnavailable(RuntimeError):
    """Жодна з моделей не дала валідної відповіді."""


class GeminiLLM:
    def __init__(
        self,
        client: genai.Client,
        models: Sequence[str],
        *,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self._client = client
        self._models: List[str] = list(models)
        self.last_model: Optional[str] = None
        # Циклічний буфер на 17 елементів, заповнений нулями в мс (1970 рік).
        self._call_times_ms: List[int] = [0] * RING_BUFFER_SIZE
        self._last_idx: int = 0
        self._rate_lock = threading.Lock()
        self._clock_ms = clock_ms
        self._sleep = sleeper

    @classmethod
    def from_api_key(cls, api_key: str, models: Sequence[str], timeout_ms: int = 60_000) -> "GeminiLLM":
        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_ms))
        return cls(client, models)

    def _wait_and_record_call(self) -> float:
        """
        Рахує кількість викликів N за останню хвилину в циклічному буфері (17 елементів).
        Час затримки = N * 0.65 с. Якщо він більший за час, що минув з останнього виклику,
        досипає різницю (delay - elapsed) і перед самим викликом записує новий час у буфер.
        Повертає фактичний час сну в секундах.
        """
        with self._rate_lock:
            now_ms = self._clock_ms()
            recent_calls = sum(1 for ts in self._call_times_ms if now_ms - ts <= WINDOW_MS)
            delay_ms = recent_calls * STEP_DELAY_MS
            last_ts = self._call_times_ms[self._last_idx]
            elapsed_ms = now_ms - last_ts

            slept_s = 0.0
            if delay_ms > 0 and delay_ms > elapsed_ms:
                wait_ms = delay_ms - elapsed_ms
                slept_s = wait_ms / 1000.0
                logger.debug("Rate limiter: %d calls in 60s -> sleeping %.2fs", recent_calls, slept_s)
                self._sleep(slept_s)
                now_ms = self._clock_ms()

            self._last_idx = (self._last_idx + 1) % RING_BUFFER_SIZE
            self._call_times_ms[self._last_idx] = now_ms
            return slept_s

    def _promote(self, model: str) -> None:
        # Робоча модель стає першою, щоб не витрачати час на ті, що падають.
        if self._models and self._models[0] != model:
            self._models.remove(model)
            self._models.insert(0, model)

    def _run(self, contents: str, config: types.GenerateContentConfig, accept: Callable) -> object:
        errors = []
        for model in list(self._models):
            try:
                self._wait_and_record_call()
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
