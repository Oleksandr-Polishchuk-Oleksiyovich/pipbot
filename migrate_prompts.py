"""
Міграція БД на нову схему промптів: створює таблиці prompts / prompt_history, записує промпти агентів
і переносить легасі bot_state.custom_system_prompt в історію (coach_dynamic, v0).

Запуск: .venv/Scripts/python.exe migrate_prompts.py [шлях_до_БД]
"""
import sys

from core.config import DB_PATH
from database.store import Store
from prompts import DEFAULT_PROMPTS


def is_corrupted(text: str) -> bool:
    # Кирилиця, записана через ANSI-пайп, перетворюється на '?'.
    return bool(text) and text.count("?") / len(text) > 0.2


def migrate(db_path: str) -> None:
    store = Store(db_path)
    store.init_schema()
    legacy = store.get_state("custom_system_prompt", "")
    if legacy and not store.prompt_history("coach_dynamic"):
        store.archive_prompt("coach_dynamic", legacy, 0, "legacy_bot_state" + (":corrupted" if is_corrupted(legacy) else ""))
        print(f"Archived legacy custom_system_prompt ({len(legacy)} chars, corrupted={is_corrupted(legacy)})")
    seeded = store.seed_prompts(DEFAULT_PROMPTS)
    print(f"{db_path}: seeded {seeded or 'nothing (already present)'}")
    for key in DEFAULT_PROMPTS:
        print(f"  {key}: v{store.get_prompt_version(key)}, {len(store.get_prompt(key) or '')} chars")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    migrate(sys.argv[1] if len(sys.argv) > 1 else DB_PATH)
