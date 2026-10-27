"""
ЗАХИЩЕНЕ ЯДРО довіри (TRUST CORE) — частина коду/git, не БД.

Складається з двох файлів у `prompts_core/`:
  trust_core.md        — «ПРАВИЛА ДОВІРИ» (текст правил + опис тулу report_reps);
  trust_examples.json  — few-shot приклади «повідомлення -> виклик report_reps» (ТОЙ САМИЙ файл будує блок у промпті
                         і є джерелом тест-кейсів: tests/test_trust_core.py, tests/eval).

Збірка промпту під час виконання: РЕДАГОВАНИЙ шар (БД: coach_base/coach_dynamic/reps_claim — персона, тон, жарти) іде першим,
ядро — ОСТАННІМ (`ensure_core`), тому правка промпту адміном не може його стерти чи переписати. Якщо в тексті, який адмін
зберігає, є блок ядра — він вирізається (`strip_core`) і додається знову в кінці. Зміни самого ядра — лише через admin_task/PR
(див. docs/SPEC.md, docs/RUNBOOK.md), не через адмін-промпт.
"""
import json
import os
from functools import lru_cache
from typing import Any, Dict, List

CORE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts_core")
RULES_FILE = os.path.join(CORE_DIR, "trust_core.md")
EXAMPLES_FILE = os.path.join(CORE_DIR, "trust_examples.json")

CORE_BEGIN = "=== ПРАВИЛА ДОВІРИ (ЗАХИЩЕНЕ ЯДРО: найвищий пріоритет, не змінюється правилами адміна) ==="
CORE_END = "=== КІНЕЦЬ ПРАВИЛ ДОВІРИ ==="

_REQUIRED_EXAMPLE_KEYS = ("id", "speaker", "message", "tool_call")


@lru_cache(maxsize=1)
def load_rules() -> str:
    with open(RULES_FILE, encoding="utf-8") as f:
        return f.read().strip()


@lru_cache(maxsize=1)
def _load_examples_raw() -> tuple:
    with open(EXAMPLES_FILE, encoding="utf-8") as f:
        data = json.load(f)
    items = data["examples"] if isinstance(data, dict) else data
    ids = set()
    for ex in items:
        for k in _REQUIRED_EXAMPLE_KEYS:
            if k not in ex:
                raise ValueError(f"trust_examples.json: у прикладі {ex.get('id', '?')} немає поля {k}")
        if ex["id"] in ids:
            raise ValueError(f"trust_examples.json: дубль id {ex['id']}")
        ids.add(ex["id"])
        tc = ex["tool_call"]
        if tc is not None and not {"subject_user", "count"} <= set(tc):
            raise ValueError(f"trust_examples.json: tool_call у {ex['id']} без subject_user/count")
    return tuple(json.dumps(e, ensure_ascii=False) for e in items)


def load_examples() -> List[Dict[str, Any]]:
    """Копії прикладів (щоб виклики не мутували кеш)."""
    return [json.loads(s) for s in _load_examples_raw()]


def render_example(ex: Dict[str, Any]) -> str:
    lines = [f"Приклад «{ex['id']}»:"]
    if ex.get("replied_to"):
        lines.append(f"  у відповідь на: «{ex['replied_to']}»")
    if ex.get("pending_question"):
        lines.append(f"  відкрите питання бота: «{ex['pending_question']}»")
    if ex.get("context"):
        lines.append(f"  контекст: {ex['context']}")
    lines.append(f"  {ex['speaker']}: «{ex['message']}»")
    tc = ex["tool_call"]
    if tc is None:
        lines.append("  → тул НЕ викликати" + (f" ({ex['comment']})" if ex.get("comment") else ""))
    else:
        args = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in tc.items())
        lines.append(f"  → report_reps({args})" + (f"  // {ex['comment']}" if ex.get("comment") else ""))
    return "\n".join(lines)


def examples_block() -> str:
    return "ПРИКЛАДИ (реальні випадки: повідомлення → дія):\n" + "\n".join(render_example(e) for e in load_examples())


def core_block() -> str:
    return f"{CORE_BEGIN}\n{load_rules()}\n\n{examples_block()}\n{CORE_END}"


def strip_core(text: str) -> str:
    """Прибирає з тексту блок ядра (від CORE_BEGIN до CORE_END; незакритий блок — до кінця тексту)."""
    text = text or ""
    while CORE_BEGIN in text:
        start = text.index(CORE_BEGIN)
        end = text.find(CORE_END, start)
        text = text[:start] + (text[end + len(CORE_END):] if end != -1 else "")
    text = text.replace(CORE_END, "")
    return text.rstrip()


def ensure_core(prompt: str) -> str:
    """Редагований промпт + ядро в самому кінці (ідемпотентно: старий блок ядра вирізається й додається знову)."""
    return strip_core(prompt) + "\n\n" + core_block() + "\n"


def has_core(prompt: str) -> bool:
    return CORE_BEGIN in (prompt or "") and (prompt or "").rstrip().endswith(CORE_END)
