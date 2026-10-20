"""
0007_prompt_template.py: Шаблон для git-міграцій промптів (T-53).
Використовується Grok Box dev agent-ом для збереження змін у coach_dynamic
чи інших промптах як єдиного джерела правди в git.
"""
import sqlite3
from database.store import apply_prompt_migration

PROMPT_KEY = "coach_dynamic"
PROMPT_CONTENT = """
🦾 **Спеціальні правила тренера (v7):**
- Завжди вітати учасників із завершенням підходу!
""".strip()
AUTHOR = "git_migration"


def upgrade(conn: sqlite3.Connection) -> None:
    """Ідемпотентно оновлює промпт у БД та архівує версію в prompt_history."""
    apply_prompt_migration(conn, PROMPT_KEY, PROMPT_CONTENT, updated_by=AUTHOR)
