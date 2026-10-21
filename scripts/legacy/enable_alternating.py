"""
Разова міграція 04.10.2026: вмикає систему чергування віджимання ↔ присідання (запропонував Льоша, погодив Саша).
04.10 — віджимання 26 для обох; 05.10 — присідання ceil(1.5 × 26) = 39; 06.10 — віджимання 27; ...
Ідемпотентна: якщо чергування вже увімкнене, нічого не робить.

Запуск: .venv/Scripts/python.exe scripts/legacy/enable_alternating.py [шлях_до_БД]  (з кореня репо, PYTHONPATH=.)
"""
import sys
from datetime import date, timedelta

from core import challenge
from core.config import DB_PATH
from database.store import Store

START = date(2026, 10, 4)
PUSHUPS = {"lyosha": 26, "sasha": 26}
SQUAT_FACTOR = 1.5


def main(db_path: str) -> None:
    store = Store(db_path)
    store.init_schema()
    if store.get_state("schedule_mode") == "alternate":
        print(f"{db_path}: alternating schedule already enabled since {store.get_state('alt_start_date')}")
    else:
        challenge.enable_alternating(store, START, PUSHUPS, SQUAT_FACTOR)
        print(f"{db_path}: alternating schedule enabled from {START}")
    for i in range(5):
        day = START + timedelta(days=i)
        print(f"  {day}: {challenge.day_exercise(store, day):8s} {challenge.day_targets(store, day)}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main(sys.argv[1] if len(sys.argv) > 1 else DB_PATH)
