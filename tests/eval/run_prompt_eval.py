#!/usr/bin/env python3
"""
ШАР ОЦІНКИ ПОВЕДІНКИ ПРОМПТІВ (live): той самий реєстр сценаріїв, але з РЕАЛЬНИМ core.llm.GeminiLLM.
НЕ входить у pytest за замовчуванням (мережеві виклики, гроші, час).

Запуск:
  python tests/eval/run_prompt_eval.py                      # skipped: need --live
  python tests/eval/run_prompt_eval.py --live               # усі сценарії, один прогін
  python tests/eval/run_prompt_eval.py --live --only id1,id2
  python tests/eval/run_prompt_eval.py --live --repeat 3    # pass-rate при недетермінізмі LLM
  python tests/eval/run_prompt_eval.py --live --json out.json

Ключ береться з .env через core.config (load_dotenv). КЛЮЧ НІКОЛИ не друкується й не логується.
Код виходу 0 лише якщо ВСІ сценарії PASS (pass-rate 1.0). Виклики послідовні з паузою (без паралелі).
"""
import argparse
import asyncio
import functools
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tests.flow_fixtures import make_flow  # noqa: E402
from tests.trust_scenarios import SCENARIOS, examples_as_scenarios, run_scenario, verify  # noqa: E402

PAUSE_S = 1.0  # пауза між сценаріями, щоб не бомбити API (додатково до rate-limiter у GeminiLLM)


class ObservingLLM:
    """Проксі над GeminiLLM: лічить, чи реально викликано report_reps (для колонки «tool call made»)."""

    def __init__(self, inner):
        self.inner = inner
        self.tool_calls = 0

    def reset(self):
        self.tool_calls = 0

    def _wrap(self, tool):
        if getattr(tool, "__name__", "") != "report_reps":
            return tool
        obs = self

        @functools.wraps(tool)
        def wrapped(*args, **kwargs):
            obs.tool_calls += 1
            return tool(*args, **kwargs)

        return wrapped

    def text(self, system, prompt, *, tools=None, temperature=0.7):
        wrapped = [self._wrap(t) for t in tools] if tools else tools
        return self.inner.text(system, prompt, tools=wrapped, temperature=temperature)

    def json(self, *a, **k):
        return self.inner.json(*a, **k)

    def vision_json(self, *a, **k):
        return self.inner.vision_json(*a, **k)


def build_live_llm():
    """Реальний GeminiLLM із .env (як bot.py). Повертає None, якщо ключа немає."""
    from core.config import CANDIDATE_MODELS, GEMINI_API_KEY
    from core.llm import GeminiLLM

    if not GEMINI_API_KEY:
        return None
    return GeminiLLM.from_api_key(GEMINI_API_KEY, CANDIDATE_MODELS)


async def _run_once(scenario, observer):
    observer.reset()
    with tempfile.TemporaryDirectory() as d:
        flow, sender, _ = make_flow(Path(d), llm=observer, trust_mode=True)
        try:
            await run_scenario(flow, sender, scenario)
            fails = verify(flow, sender, scenario)
        except Exception as e:  # noqa: BLE001
            fails = [f"EXCEPTION: {type(e).__name__}: {e}"]
    return fails, observer.tool_calls


def evaluate(scenarios, observer, repeat):
    results = []
    for sc in scenarios:
        passes, last_fail, tool_made = 0, "", False
        for _ in range(repeat):
            fails, tool_calls = asyncio.run(_run_once(sc, observer))
            tool_made = tool_made or tool_calls > 0
            if not fails:
                passes += 1
            else:
                last_fail = fails[0]
            time.sleep(PAUSE_S)
        results.append({
            "id": sc.id, "category": sc.category, "passes": passes, "runs": repeat,
            "tool_made": tool_made, "reason": "" if passes == repeat else last_fail,
        })
    return results


def print_table(results, repeat):
    print(f"\n{'VERDICT':8} {'tool':4} {'rate':6} {'id':34} {'category':24} reason")
    print("-" * 110)
    ok = 0
    for r in results:
        verdict = "PASS" if r["passes"] == repeat else "FAIL"
        ok += 1 if verdict == "PASS" else 0
        rate = f"{r['passes']}/{r['runs']}"
        tool = "yes" if r["tool_made"] else "no"
        print(f"{verdict:8} {tool:4} {rate:6} {r['id']:34} {r['category']:24} {r['reason'][:48]}")
    print("-" * 110)
    print(f"ПІДСУМОК: {ok}/{len(results)} PASS")
    return ok == len(results)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Live prompt-behaviour eval for TRUST_MODE scenarios")
    parser.add_argument("--live", action="store_true", help="робити реальні виклики GeminiLLM")
    parser.add_argument("--only", default="", help="кома-розділений список id сценаріїв")
    parser.add_argument("--repeat", type=int, default=1, help="прогонів на сценарій (pass-rate)")
    parser.add_argument("--json", default="", help="шлях, куди зберегти результати JSON")
    args = parser.parse_args(argv)

    if not args.live:
        print("skipped: need --live (мережеві виклики GeminiLLM вимкнені за замовчуванням)")
        return 0

    scenarios = list(SCENARIOS) + examples_as_scenarios()
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        scenarios = [s for s in scenarios if s.id in wanted]
        if not scenarios:
            print(f"немає сценаріїв з id у {sorted(wanted)}")
            return 2

    inner = build_live_llm()
    if inner is None:
        print("GEMINI_API_KEY порожній — нема чим робити live-прогін (ключ не друкуємо).")
        return 2

    observer = ObservingLLM(inner)
    results = evaluate(scenarios, observer, max(1, args.repeat))
    all_pass = print_table(results, max(1, args.repeat))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"JSON збережено: {args.json}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
