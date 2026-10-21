"""
Offline tests for Phase 1:
- T-10: Migration runner, schema_version, backup, legacy stamping, idempotency, CLI
- T-11: WAL, busy_timeout, foreign_keys pragmas, DB_PATH config
- T-12: Legacy progress backfill to user_progress, no writes to progress, aliases from users
- T-13: pending_replies table, add/list/mark, startup drain idempotency and failure retries
"""
import os
import sqlite3
import subprocess
import sys
from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.flow import ChatFlow
from core import challenge
from core.config import BASE_DIR, DB_PATH
from database.migrate import (
    get_applied_migrations,
    get_current_version,
    get_migration_status,
    run_migrations,
)
from database.store import Store, User
from tests.flow_fixtures import Clock, FakeSender, NOW


# ============================================================================
# T-10: Migration runner + schema_version + backup
# ============================================================================

def test_clean_db_applies_all_migrations(tmp_path):
    """На чистій БД runner створює всі таблиці й ставить версію HEAD (v9)."""
    db_file = str(tmp_path / "clean.db")
    applied = run_migrations(db_file)
    assert len(applied) == 9
    assert [v for v, _ in applied] == [1, 2, 3, 4, 5, 6, 7, 8, 9]

    assert get_current_version(db_file) == 9

    # Перевіряємо наявність таблиць
    conn = sqlite3.connect(db_file)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    conn.close()
    expected = {
        "schema_version", "progress", "users", "user_progress",
        "chat_history", "bot_state", "prompts", "prompt_history", "pending_replies",
        "video_checks", "offload_requests", "admin_audit", "bot_texts",
    }
    assert expected.issubset(tables)


def test_migrations_idempotency(tmp_path):
    """Повторний запуск runner — no-op, повертає порожній список."""
    db_file = str(tmp_path / "idem.db")
    first = run_migrations(db_file)
    assert len(first) == 9

    second = run_migrations(db_file)
    assert second == []
    assert get_current_version(db_file) == 9


def test_legacy_db_stamped_v1_without_data_loss(tmp_path):
    """На існуючій legacy-БД (з users і progress, без schema_version) — stamp v1 без втрати даних."""
    db_file = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_file)
    # Створюємо базову схему вручну без schema_version
    conn.executescript("""
        CREATE TABLE progress (
            date TEXT PRIMARY KEY,
            lyosha_done INTEGER DEFAULT 0,
            sasha_done INTEGER DEFAULT 0
        );
        CREATE TABLE users (
            slug TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE user_progress (
            date TEXT NOT NULL,
            user_slug TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (date, user_slug)
        );
        INSERT INTO users (slug, display_name, status) VALUES ('lyosha', 'Льоша', 'active');
        INSERT INTO users (slug, display_name, status) VALUES ('sasha', 'Саша', 'active');
        INSERT INTO progress (date, lyosha_done, sasha_done) VALUES ('2026-09-01', 1, 0);
    """)
    conn.commit()
    conn.close()

    # Запускаємо runner
    applied = run_migrations(db_file)
    # Версія 1 має бути застампована як legacy, а версії 2..8 застосовані
    applied_versions = [v for v, _ in applied]
    assert applied_versions == [2, 3, 4, 5, 6, 7, 8, 9]

    applied_all = get_applied_migrations(db_file)
    assert [v for v, _, _ in applied_all] == [1, 2, 3, 4, 5, 6, 7, 8, 9]

    # Перевіряємо збереження історичних даних і результат міграції v2
    conn = sqlite3.connect(db_file)
    users_count = conn.execute("SELECT count(*) FROM users").fetchone()[0]
    assert users_count == 2

    # Дані з progress мігровані у user_progress
    up_rows = conn.execute("SELECT date, user_slug, done FROM user_progress ORDER BY user_slug").fetchall()
    assert ("2026-09-01", "lyosha", 1) in up_rows
    assert ("2026-09-01", "sasha", 0) in up_rows
    conn.close()


def test_backup_created_before_migrations(tmp_path):
    """Перед застосуванням міграцій створюється online backup БД."""
    db_file = str(tmp_path / "with_backup.db")
    backup_dir = str(tmp_path / "custom_backups")

    # Створюємо БД з baseline
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE users (slug TEXT PRIMARY KEY, display_name TEXT);")
    conn.execute("INSERT INTO users VALUES ('test', 'Test User');")
    conn.commit()
    conn.close()

    run_migrations(db_file, backup_dir=backup_dir)

    assert os.path.exists(backup_dir)
    backup_files = [f for f in os.listdir(backup_dir) if f.endswith(".db")]
    assert len(backup_files) >= 1

    # Перевіряємо що backup-файл валідний і містить дані
    b_conn = sqlite3.connect(os.path.join(backup_dir, backup_files[0]))
    b_users = b_conn.execute("SELECT slug FROM users").fetchall()
    b_conn.close()
    assert b_users == [("test",)]


def test_cli_status_and_migration(tmp_path):
    """CLI python -m database.migrate працює з параметрами --status та --db."""
    db_file = str(tmp_path / "cli.db")

    # Status на новій БД
    status_cmd = [sys.executable, "-m", "database.migrate", "--db", db_file, "--status"]
    res = subprocess.run(status_cmd, capture_output=True, text=True, check=True)
    assert "Current schema version: 0" in res.stdout
    assert "Pending migrations:" in res.stdout

    # Запуск міграцій
    mig_cmd = [sys.executable, "-m", "database.migrate", "--db", db_file]
    res_mig = subprocess.run(mig_cmd, capture_output=True, text=True, check=True)
    assert "Applied 9 migration(s)" in res_mig.stdout

    # Status після міграцій
    res_after = subprocess.run(status_cmd, capture_output=True, text=True, check=True)
    assert "Current schema version: 9" in res_after.stdout
    assert "none - up to date" in res_after.stdout


# ============================================================================
# T-11: WAL, busy_timeout, foreign_keys pragmas, DB_PATH
# ============================================================================

def test_store_connect_pragmas(tmp_path):
    """Store._connect відкриває з'єднання з journal_mode=WAL, busy_timeout=5000, foreign_keys=ON."""
    db_file = str(tmp_path / "pragma.db")
    store = Store(db_file)
    store.init_schema()

    with store._connect() as con:
        mode = con.execute("PRAGMA journal_mode;").fetchone()[0]
        timeout = con.execute("PRAGMA busy_timeout;").fetchone()[0]
        fk = con.execute("PRAGMA foreign_keys;").fetchone()[0]

    assert mode.lower() == "wal"
    assert timeout == 5000
    assert fk == 1


def test_foreign_keys_enforced(tmp_path):
    """Перевіряємо, що foreign_keys=ON дійсно контролює цілісність зв'язків."""
    db_file = str(tmp_path / "fk.db")
    store = Store(db_file)
    store.init_schema()

    # Спроба вставити user_progress для неіснуючого user_slug повинна викинути помилку
    with pytest.raises(sqlite3.IntegrityError):
        with store._connect() as con:
            con.execute("INSERT INTO user_progress (date, user_slug, done) VALUES ('2026-10-20', 'non_existent', 1)")


def test_db_path_default():
    """DB_PATH локально за замовчуванням знаходиться у BASE_DIR/challenge.db."""
    assert "challenge.db" in DB_PATH
    assert not DB_PATH.startswith("/var/lib")  # VM-шлях не захардкоджений у дефолт


# ============================================================================
# T-12: Data-migration legacy progress → user_progress + aliases
# ============================================================================

def test_progress_backfill_and_read_fallback(tmp_path):
    """
    Після міграції v2 історичний прогрес мігрується в user_progress;
    get_day_progress повертає правильні значення;
    нові set_done не пишуть у таблицю progress.
    """
    db_file = str(tmp_path / "progress_mig.db")
    store = Store(db_file)
    store.init_schema()

    # Створюємо користувачів
    store.upsert_user(User(slug="lyosha", display_name="Льоша", status="active"))
    store.upsert_user(User(slug="sasha", display_name="Саша", status="active"))

    # Записуємо старі дані в legacy progress таблицю напряму
    with store._connect() as con:
        con.execute("INSERT INTO progress (date, lyosha_done, sasha_done) VALUES ('2026-09-10', 1, 0)")
        con.execute("INSERT INTO progress (date, lyosha_done, sasha_done) VALUES ('2026-09-11', 1, 1)")

    # Проганяємо міграції
    run_migrations(db_file)

    # Перевіряємо що get_day_progress для старих дат збігається
    p1 = store.get_day_progress("2026-09-10")
    assert p1["lyosha"] is True
    assert p1["sasha"] is False

    p2 = store.get_day_progress("2026-09-11")
    assert p2["lyosha"] is True
    assert p2["sasha"] is True

    # Новий запис через set_done
    store.set_done("lyosha", True, "2026-10-20")
    store.set_done("sasha", True, "2026-10-20")

    # У user_progress з'явилися нові записи
    assert store.get_user_done("lyosha", "2026-10-20") is True
    assert store.get_user_done("sasha", "2026-10-20") is True

    # Таблиця progress НЕ отримує нових записів
    with store._connect() as con:
        row = con.execute("SELECT * FROM progress WHERE date = '2026-10-20'").fetchone()
    assert row is None


def test_normalize_participant_uses_users_table(tmp_path):
    """normalize_participant використовує дані з таблиці users, без hardcoded aliases."""
    db_file = str(tmp_path / "norm.db")
    store = Store(db_file)
    store.init_schema()

    store.upsert_user(User(
        slug="ivan",
        display_name="Іван",
        aliases=["іванко", "вано", "vanya"],
        status="active"
    ))

    # Перевіряємо пошук динамічного юзера
    assert challenge.normalize_participant("іванко", store) == "ivan"
    assert challenge.normalize_participant("вано", store) == "ivan"
    assert challenge.normalize_participant("Vanya", store) == "ivan"
    assert challenge.normalize_participant("Іван", store) == "ivan"
    assert challenge.normalize_participant("невідомий", store) is None

    # Без передачі store функція не знаходить псевдонімів
    assert challenge.normalize_participant("іванко", None) is None


# ============================================================================
# T-13: pending_replies + drain on startup
# ============================================================================

@pytest.mark.asyncio
async def test_pending_replies_store_methods(tmp_path):
    """Методи add_pending_reply, list_pending_replies, mark_reply_sent, increment_reply_attempts."""
    db_file = str(tmp_path / "pending.db")
    store = Store(db_file)
    store.init_schema()

    # Додаємо запис
    store.add_pending_reply("req-001", 1001, "Привіт після деплою", reply_to_message_id=55)

    # Ідемпотентність: повторний виклик з тим самим request_id не дублює
    store.add_pending_reply("req-001", 1001, "Дублікат", reply_to_message_id=55)

    pending = store.list_pending_replies("pending")
    assert len(pending) == 1
    assert pending[0].request_id == "req-001"
    assert pending[0].chat_id == 1001
    assert pending[0].reply_to_message_id == 55
    assert pending[0].text == "Привіт після деплою"
    assert pending[0].attempts == 0

    # Позначаємо як sent
    store.mark_reply_sent("req-001")
    assert store.list_pending_replies("pending") == []
    sent = store.list_pending_replies("sent")
    assert len(sent) == 1
    assert sent[0].request_id == "req-001"


@pytest.mark.asyncio
async def test_pending_replies_drain_idempotent(tmp_path):
    """Після імітованого restart pending-reply надсилається рівно раз; повторний запуск не дублює."""
    clock = Clock(NOW)
    sender = FakeSender()
    store = Store(str(tmp_path / "drain.db"))
    store.init_schema()

    flow = ChatFlow(store, None, sender, now=clock)

    # Додаємо дві очікуючі відповіді
    store.add_pending_reply("req-101", 1001, "Деплой завершено успішно")
    store.add_pending_reply("req-102", 1001, "Всі норми перераховано")

    # Перший запуск drain
    sent_count = await flow.drain_pending_replies()
    assert sent_count == 2
    assert sender.texts == [
        "Деплой завершено успішно",
        "Всі норми перераховано",
    ]

    # Повторний запуск drain (симуляція ще одного старту/виклику)
    sent_again = await flow.drain_pending_replies()
    assert sent_again == 0
    # Кількість надісланих повідомлень не змінилась
    assert len(sender.texts) == 2


@pytest.mark.asyncio
async def test_pending_replies_drain_on_startup_trigger(tmp_path):
    """При trigger='startup' flow.process автоматично викликає drain_pending_replies."""
    clock = Clock(NOW)
    sender = FakeSender()
    store = Store(str(tmp_path / "startup_drain.db"))
    store.init_schema()
    flow = ChatFlow(store, None, sender, now=clock)

    store.add_pending_reply("req-startup", 1001, "Повідомлення на старті")

    await flow.process(1001, trigger="startup")
    assert sender.texts == ["Повідомлення на старті"]
    assert store.list_pending_replies("pending") == []


@pytest.mark.asyncio
async def test_pending_replies_retry_on_failure(tmp_path):
    """При помилці надсилання лічильник attempts збільшується, а при перевищенні max_attempts статус стає 'failed'."""
    clock = Clock(NOW)

    class FailingSender:
        def __init__(self):
            self.calls = 0

        async def send_text(self, chat_id, text, **kwargs):
            self.calls += 1
            raise RuntimeError("Telegram network error")

        async def send_video(self, chat_id, path):
            pass

    sender = FailingSender()
    store = Store(str(tmp_path / "fail_drain.db"))
    store.init_schema()
    flow = ChatFlow(store, None, sender, now=clock)

    store.add_pending_reply("req-fail", 1001, "Тест помилки")

    # Спроба 1 (attempts -> 1, status pending)
    await flow.drain_pending_replies(max_attempts=3)
    p = store.list_pending_replies("pending")
    assert len(p) == 1
    assert p[0].attempts == 1

    # Спроба 2 (attempts -> 2, status pending)
    await flow.drain_pending_replies(max_attempts=3)
    p = store.list_pending_replies("pending")
    assert len(p) == 1
    assert p[0].attempts == 2

    # Спроба 3 (attempts -> 3, status -> failed)
    await flow.drain_pending_replies(max_attempts=3)
    assert store.list_pending_replies("pending") == []
    failed = store.list_pending_replies("failed")
    assert len(failed) == 1
    assert failed[0].attempts == 3
