"""
Хелсчек Gemini — тієї ж обгортки GeminiLLM, якою користуються агенти бота.
  1. ключ валідний і API віддає список моделей;
  2. які моделі з CANDIDATE_MODELS реально доступні;
  3. генерація тексту (як у тренера);
  4. структурована JSON-відповідь за схемою (як у роутера/судді).

Запуск:  .venv/Scripts/python.exe healthcheck.py [--all-models]
  --all-models  — прогнати генерацію на кожній доступній моделі (повільніше).
Код виходу: 0 — все OK, 1 — є проблеми.
"""
import sys
import time

import httpx
from pydantic import BaseModel

from core.config import CANDIDATE_MODELS, GEMINI_API_KEY
from core.llm import GeminiLLM, LLMUnavailable

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


class Ping(BaseModel):
    ok: bool
    word: str


def mask(key: str) -> str:
    return f"{key[:4]}…{key[-4:]} (len={len(key)})" if len(key) > 8 else "<too short>"


def timed(fn):
    t = time.perf_counter()
    result = fn()
    return result, (time.perf_counter() - t) * 1000


def available_models() -> list:
    resp, ms = timed(lambda: httpx.get(f"{GEMINI_BASE}/models", headers={"x-goog-api-key": GEMINI_API_KEY},
                                       params={"pageSize": 1000}, timeout=20))
    if resp.status_code != 200:
        raise RuntimeError(f"list models -> HTTP {resp.status_code}: {resp.text[:200]}")
    names = {m["name"].removeprefix("models/") for m in resp.json().get("models", [])}
    print(f"  OK   list models: {len(names)} моделей ({ms:.0f} ms)")
    present = [m for m in CANDIDATE_MODELS if m in names]
    missing = [m for m in CANDIDATE_MODELS if m not in names]
    print(f"  OK   доступні кандидати: {present}")
    if missing:
        print(f"  WARN недоступні кандидати (варто прибрати з CANDIDATE_MODELS): {missing}")
    if not present:
        raise RuntimeError("жодна модель з CANDIDATE_MODELS не доступна")
    return present


def check_text(llm: GeminiLLM) -> None:
    text, ms = timed(lambda: llm.text("Відповідай одним словом.", "Скажи слово: pong", temperature=0))
    print(f"  OK   text [{llm.last_model}]: {text!r} ({ms:.0f} ms)")


def check_json(llm: GeminiLLM) -> None:
    ping, ms = timed(lambda: llm.json("Return the requested JSON.", 'Set ok=true and word="pong".', Ping))
    if not ping.ok:
        raise RuntimeError(f"unexpected JSON payload: {ping}")
    print(f"  OK   json [{llm.last_model}]: {ping.model_dump()} ({ms:.0f} ms)")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("== Gemini ==")
    if not GEMINI_API_KEY:
        print("  FAIL GEMINI_API_KEY порожній")
        return 1
    print(f"  key: {mask(GEMINI_API_KEY)}")

    failures = 0
    try:
        present = available_models()
    except Exception as e:
        print(f"  FAIL {e}")
        return 1

    llm = GeminiLLM.from_api_key(GEMINI_API_KEY, present, timeout_ms=30_000)
    for check in (check_text, check_json):
        try:
            check(llm)
        except (LLMUnavailable, RuntimeError) as e:
            print(f"  FAIL {check.__name__}: {e}")
            failures += 1

    if "--all-models" in sys.argv:
        for model in present:
            try:
                _, ms = timed(lambda: GeminiLLM.from_api_key(GEMINI_API_KEY, [model], 30_000).text("", "pong?", temperature=0))
                print(f"  OK   {model} ({ms:.0f} ms)")
            except LLMUnavailable as e:
                print(f"  WARN {model}: {str(e)[:150]}")

    print(f"\nSUMMARY: gemini={'OK' if not failures else 'FAIL'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
