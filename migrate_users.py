"""
Міграція учасників та прогресу:
1. Вивчає дані про Сашу і Льошу в bot_state, progress та prompts (coach_dynamic).
2. Створює повноцінні сутності в таблиці users зі збереженням поточних життів (lives),
   максимумів життів (max_lives: Льоша=4, Саша=7), базових норм, зсувів, описів та псевдонімів.
3. Переносить історію виконання з таблиці progress (lyosha_done, sasha_done) у нормалізовану user_progress.
4. Оновлює дефолтні промпти агентів (включно з новим промптом 'onboarding') та прибирає хардкод імен із coach_dynamic.

Запуск: .venv/Scripts/python.exe migrate_users.py [шлях_до_БД]
"""
import re
import sys
from typing import Optional

from core.config import DB_PATH, START_DATE
from database.store import Store, User

LYOSHA_ALIASES = [
    "льош", "леш", "lyosha", "alexey", "oleksiy", "олекс", "алекс",
    "син", "малий", "сонцелик", "алексей", "олексій",
]
SASHA_ALIASES = [
    "саш", "саня", "sasha", "oleksandr", "олександр", "александр",
    "тато", "батько", "polishchuk", "oleksandr polishchuk",
]


def _parse_int(raw: str) -> Optional[int]:
    s = (raw or "").strip()
    return int(s) if s and s.lstrip("-").isdigit() else None


def ensure_default_users(store: Store) -> list[str]:
    """
    Ідемпотентно створює Сашу та Льошу в таблиці users із даних bot_state та промптів,
    якщо їх ще немає в таблиці users, і переносить записи з progress у user_progress.
    """
    store.init_schema()
    state = store.all_state()
    dynamic_prompt = store.get_prompt("coach_dynamic") or ""

    lyosha_desc = "Син, молодший учасник челенджу. Тон тренера: тепло й підбадьорливо («Красава, тримай темп!»)."
    if "сонцеликий" in dynamic_prompt.lower():
        lyosha_desc += " Титул: Сонцеликий Льоша."

    sasha_desc = "Тато, старший учасник челенджу. Тон тренера: стримано, по-чоловічому («Норма закрита чітко. Відпочинок заслужено!»)."

    created = []
    defaults = [
        User(
            slug="lyosha",
            telegram_id=None,
            telegram_name="Алексей",
            username="",
            display_name="Льоша",
            emoji="👦",
            description=lyosha_desc,
            aliases=LYOSHA_ALIASES,
            status="active",
            difficulty="custom",
            lives=_parse_int(state.get("lives_lyosha", "")),
            max_lives=4,
            base_pushups=3,
            ladder_offset=_parse_int(state.get("offset_lyosha", "")) or 0,
            alt_pushups=_parse_int(state.get("alt_pushups_lyosha", "")),
            joined_date=START_DATE.isoformat(),
        ),
        User(
            slug="sasha",
            telegram_id=None,
            telegram_name="Oleksandr Polishchuk",
            username="",
            display_name="Саша",
            emoji="👨",
            description=sasha_desc,
            aliases=SASHA_ALIASES,
            status="active",
            difficulty="custom",
            lives=_parse_int(state.get("lives_sasha", "")),
            max_lives=7,
            base_pushups=13,
            ladder_offset=_parse_int(state.get("offset_sasha", "")) or 0,
            alt_pushups=_parse_int(state.get("alt_pushups_sasha", "")),
            joined_date=START_DATE.isoformat(),
        ),
    ]

    for u in defaults:
        existing = store.get_user(u.slug)
        if existing is None:
            store.upsert_user(u)
            created.append(u.slug)
        else:
            # Якщо користувач уже є, але в bot_state оновились легасі-лічильники, підтягуємо їх за потреби
            changed = False
            if existing.lives is None and u.lives is not None:
                existing.lives = u.lives
                changed = True
            if existing.alt_pushups is None and u.alt_pushups is not None:
                existing.alt_pushups = u.alt_pushups
                changed = True
            if changed:
                store.upsert_user(existing)

    # Переносимо історичний прогрес із таблиці progress (date, lyosha_done, sasha_done) у user_progress
    with store._connect() as con:
        legacy_rows = con.execute("SELECT date, lyosha_done, sasha_done FROM progress").fetchall()
        for day_iso, l_done, s_done in legacy_rows:
            con.execute(
                "INSERT OR IGNORE INTO user_progress (date, user_slug, done) VALUES (?, 'lyosha', ?)",
                (day_iso, int(bool(l_done))),
            )
            con.execute(
                "INSERT OR IGNORE INTO user_progress (date, user_slug, done) VALUES (?, 'sasha', ?)",
                (day_iso, int(bool(s_done))),
            )

    return created


def migrate_dynamic_prompt(store: Store) -> bool:
    """
    Прибирає з coach_dynamic жорстку прив'язку лише до двох учасників («Льоша (X/4) | Саша (Y/7)»),
    замінюючи її на універсальне правило для всіх активних учасників, зі збереженням кастомних правил адміна.
    """
    current = store.get_prompt("coach_dynamic") or ""
    if not current:
        return False

    updated = current
    # Оновлюємо старий рядок статусу з фіксованими двома іменами на універсальний для всіх учасників
    updated = re.sub(
        r"\*Життя:\s*(?:Сонцеликий\s+)?Льоша\s*\(X/4\)\s*\|\s*Саша\s*\(Y/7\)\*",
        "*Життя: <Ім'я> (X/макс) | ...* для всіх активних учасників",
        updated,
    )
    updated = re.sub(
        r"максимум\s*—\s*Льоша\s*\*\*4\*\*,\s*Саша\s*\*\*7\*\*\.",
        "максимум життів кожного учасника вказано в його профілі в блоці стану.",
        updated,
    )
    if updated != current:
        ver = store.get_prompt_version("coach_dynamic")
        store.archive_prompt("coach_dynamic", current, ver, "pre_multiuser_migration")
        store.save_prompt("coach_dynamic", updated, "migration")
        return True
    return False


def migrate(db_path: str) -> None:
    from prompts import DEFAULT_PROMPTS

    store = Store(db_path)
    store.init_schema()
    created = ensure_default_users(store)
    seeded = store.seed_prompts(DEFAULT_PROMPTS)
    dyn_updated = migrate_dynamic_prompt(store)

    users = store.list_users()
    print(f"DB {db_path}: created users={created}, total users={len(users)}, seeded prompts={seeded}, coach_dynamic_migrated={dyn_updated}")
    for u in users:
        print(
            f"  [{u.slug}] {u.emoji} {u.display_name} (tg={u.telegram_name!r}, status={u.status}, "
            f"lives={u.lives}/{u.max_lives}, base={u.base_pushups}, offset={u.ladder_offset}, alt={u.alt_pushups})"
        )


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    migrate(sys.argv[1] if len(sys.argv) > 1 else DB_PATH)
