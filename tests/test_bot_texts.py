"""
Офлайн-тести для bot_texts: міграція 0008, Store CRUD, кешований BotTexts сервіс,
адмінська правка без деплою та тон-зразок для Gemini (без мережі — fake LLM).
"""
import importlib.util
import os
import sqlite3

import pytest

from core.bot_texts import BotTexts
from database.migrate import run_migrations
from database.store import Store
from prompts import DEFAULT_BOT_TEXTS, REDO_WORDS
from tests.flow_fixtures import CHAT, make_flow

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


def _load_migration_upgrade(name: str):
    path = os.path.join(MIGRATIONS_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"migrations.{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.upgrade


class FakeLLM:
    """Синхронний fake для GeminiLLM.text — без мережі, викликається через asyncio.to_thread."""

    def __init__(self, response=None, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.calls = []

    def text(self, system, prompt, *, tools=None, temperature=0.7):
        self.calls.append((system, prompt))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


# ============================================================================
# Міграція 0008_bot_texts
# ============================================================================

def test_migration_creates_table_seeds_defaults_and_migrates_bot_state(tmp_path):
    db_file = str(tmp_path / "seed.db")
    conn = sqlite3.connect(db_file)
    conn.execute("""
        CREATE TABLE bot_state (key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
    """)
    conn.execute("INSERT INTO bot_state (key, value) VALUES ('text:angle_hint', 'Легасі підказка про ракурс')")
    conn.execute("INSERT INTO bot_state (key, value) VALUES ('other_key', 'не чіпати')")
    conn.commit()

    upgrade = _load_migration_upgrade("0008_bot_texts")
    upgrade(conn)
    conn.commit()

    rows = dict(conn.execute("SELECT key, text FROM bot_texts").fetchall())
    for key, text in DEFAULT_BOT_TEXTS.items():
        if key == "angle_hint":
            continue
        assert rows[key] == text

    # Легасі bot_state 'text:angle_hint' переносить значення, перезаписуючи сід
    assert rows["angle_hint"] == "Легасі підказка про ракурс"

    # Рядок text:% видалено з bot_state, а інші ключі лишились
    remaining = dict(conn.execute("SELECT key, value FROM bot_state").fetchall())
    assert "text:angle_hint" not in remaining
    assert remaining["other_key"] == "не чіпати"
    conn.close()


def test_migration_seed_never_overwrites_admin_edit(tmp_path):
    db_file = str(tmp_path / "idem.db")
    conn = sqlite3.connect(db_file)
    upgrade = _load_migration_upgrade("0008_bot_texts")
    upgrade(conn)
    conn.commit()

    conn.execute("UPDATE bot_texts SET text = 'Адмінський текст', updated_by = 'admin' WHERE key = 'success'")
    conn.commit()

    # Повторний виклик (ідемпотентність) не повинен перезаписати адмінську правку
    upgrade(conn)
    conn.commit()

    text, updated_by = conn.execute("SELECT text, updated_by FROM bot_texts WHERE key = 'success'").fetchone()
    assert text == "Адмінський текст"
    assert updated_by == "admin"
    conn.close()


def test_run_migrations_end_to_end_creates_bot_texts(tmp_path):
    db_file = str(tmp_path / "full.db")
    applied = run_migrations(db_file)
    assert "0008_bot_texts" in [name for _, name in applied]

    store = Store(db_file)
    for key, text in DEFAULT_BOT_TEXTS.items():
        row = store.get_bot_text(key)
        assert row is not None
        assert row["text"] == text


# ============================================================================
# Store CRUD
# ============================================================================

def test_store_bot_text_get_set_list(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.init_schema()

    assert store.get_bot_text("success") is None

    store.set_bot_text("success", "Текст {reps}/{target}", variants=["v1", "v2"], updated_by="admin")
    row = store.get_bot_text("success")
    assert row["text"] == "Текст {reps}/{target}"
    assert row["variants"] == ["v1", "v2"]
    assert row["updated_by"] == "admin"

    keys = {r["key"] for r in store.list_bot_texts()}
    assert "success" in keys


def test_store_seed_bot_texts_is_idempotent(tmp_path):
    store = Store(str(tmp_path / "seed2.db"))
    store.init_schema()

    seeded = store.seed_bot_texts(DEFAULT_BOT_TEXTS)
    assert set(seeded) == set(DEFAULT_BOT_TEXTS)

    store.set_bot_text("angle_hint", "Адмінський текст", updated_by="admin")
    seeded_again = store.seed_bot_texts(DEFAULT_BOT_TEXTS)
    assert "angle_hint" not in seeded_again
    assert store.get_bot_text("angle_hint")["text"] == "Адмінський текст"


# ============================================================================
# core.bot_texts.BotTexts
# ============================================================================

def test_bot_texts_db_override_and_default_fallback(tmp_path):
    store = Store(str(tmp_path / "bt.db"))
    store.init_schema()
    bt = BotTexts(store)

    assert bt.get("angle_hint") == DEFAULT_BOT_TEXTS["angle_hint"]

    store.set_bot_text("angle_hint", "Кастомний текст")
    bt.invalidate("angle_hint")
    assert bt.get("angle_hint") == "Кастомний текст"


def test_bot_texts_params_formatting_is_safe(tmp_path):
    store = Store(str(tmp_path / "bt2.db"))
    store.init_schema()
    bt = BotTexts(store)

    assert bt.get("success", reps=38, target=37, exercise="віджимань") == DEFAULT_BOT_TEXTS["success"].format(reps=38, target=37, exercise="віджимань")

    # Відсутній параметр не ламає форматування — плейсхолдер лишається як є, без винятку
    text = bt.get("success")
    assert "{reps}" in text and "{target}" in text


def test_bot_texts_cache_invalidation_after_set(tmp_path):
    store = Store(str(tmp_path / "bt3.db"))
    store.init_schema()
    bt = BotTexts(store)

    assert bt.get("processing") == DEFAULT_BOT_TEXTS["processing"]

    bt.set("processing", "Новий текст обробки", updated_by="admin")
    assert bt.get("processing") == "Новий текст обробки"

    # Інший запис у ту саму БД (інший процес/інстанс) + reload() також скидає кеш
    store.set_bot_text("processing", "Ще інший текст", updated_by="admin2")
    bt.reload()
    assert bt.get("processing") == "Ще інший текст"


def test_default_bot_texts_never_ask_to_redo():
    for key, text in DEFAULT_BOT_TEXTS.items():
        assert not any(w in text.lower() for w in REDO_WORDS), key


# ============================================================================
# Адмінська правка діє негайно (без деплою/рестарту) + відео-флоу
# ============================================================================

@pytest.mark.asyncio
async def test_admin_set_bot_text_takes_effect_immediately_in_next_video_reply(tmp_path):
    flow, sender, _ = make_flow(tmp_path, llm=None)

    actions = []
    tools = flow.admin._tools(today=flow.now().date(), author="oleksandr", actions=actions)
    set_bot_text_tool = next(t for t in tools if t.__name__ == "set_bot_text")
    list_bot_texts_tool = next(t for t in tools if t.__name__ == "list_bot_texts")

    result = set_bot_text_tool(key="angle_hint", text="Нова підказка про ракурс 🎥")
    assert "angle_hint" in result
    assert "angle_hint" in list_bot_texts_tool()

    flow.store.create_video_check(
        request_id="req-admin-edit",
        chat_id=CHAT,
        message_id=900,
        user_slug="lyosha",
        duration_s=42.0,
        exercise="pushup",
        reps=40,
        confidence=0.55,
        valid=True,
        status="done",
        flags=["low_camera_angle"],
    )
    reply = await flow.handle_video_callback("req-admin-edit")
    assert "Нова підказка про ракурс" in reply
    assert "angle_hint" in actions[0] or any("set_bot_text" in a for a in actions)


@pytest.mark.asyncio
async def test_low_camera_angle_credits_reps_and_appends_angle_hint(tmp_path):
    """Погана/низька камера ніколи не блокує зарахування; підказка береться з BotTexts."""
    flow, sender, _ = make_flow(tmp_path, llm=None)
    flow.store.create_video_check(
        request_id="req-angle",
        chat_id=CHAT,
        message_id=901,
        user_slug="lyosha",
        duration_s=42.0,
        exercise="pushup",
        reps=40,
        confidence=0.55,
        valid=True,
        status="done",
        flags=["low_camera_angle"],
    )
    reply = await flow.handle_video_callback("req-angle")
    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is True
    assert DEFAULT_BOT_TEXTS["angle_hint"] in reply
    assert not any(w in reply.lower() for w in REDO_WORDS)


# ============================================================================
# Тон-зразок для Gemini (paraphrase) — fake LLM, без мережі
# ============================================================================

@pytest.mark.asyncio
async def test_video_reply_paraphrase_used_when_llm_available(tmp_path):
    fake_llm = FakeLLM(response="Побачив 38 віджимань з 37 — машина, норму зараховано!")
    flow, sender, _ = make_flow(tmp_path, llm=fake_llm)
    flow.store.create_video_check(
        request_id="req-para-ok", chat_id=CHAT, message_id=902, user_slug="lyosha",
        duration_s=45.0, exercise="pushup", reps=38, confidence=0.92, valid=True, status="done",
    )
    reply = await flow.handle_video_callback("req-para-ok")
    assert reply == "Побачив 38 віджимань з 37 — машина, норму зараховано!"
    assert fake_llm.calls
    fixed_text = DEFAULT_BOT_TEXTS["success"].format(reps=38, target=37, exercise="віджимань")
    assert fixed_text in fake_llm.calls[0][1]


@pytest.mark.asyncio
async def test_video_reply_paraphrase_falls_back_on_redo_wording(tmp_path):
    fake_llm = FakeLLM(response="Будь ласка, зніми ще раз цей підхід")
    flow, sender, _ = make_flow(tmp_path, llm=fake_llm)
    flow.store.create_video_check(
        request_id="req-para-redo", chat_id=CHAT, message_id=903, user_slug="lyosha",
        duration_s=45.0, exercise="pushup", reps=38, confidence=0.92, valid=True, status="done",
    )
    reply = await flow.handle_video_callback("req-para-redo")
    assert reply == DEFAULT_BOT_TEXTS["success"].format(reps=38, target=37, exercise="віджимань")


@pytest.mark.asyncio
async def test_video_reply_paraphrase_falls_back_on_llm_error(tmp_path):
    fake_llm = FakeLLM(raise_exc=RuntimeError("network down"))
    flow, sender, _ = make_flow(tmp_path, llm=fake_llm)
    flow.store.create_video_check(
        request_id="req-para-err", chat_id=CHAT, message_id=904, user_slug="lyosha",
        duration_s=45.0, exercise="pushup", reps=38, confidence=0.92, valid=True, status="done",
    )
    reply = await flow.handle_video_callback("req-para-err")
    assert reply == DEFAULT_BOT_TEXTS["success"].format(reps=38, target=37, exercise="віджимань")


@pytest.mark.asyncio
async def test_video_reply_paraphrase_rejected_when_numbers_change(tmp_path):
    """Gemini-парафраз зі зміненими числами відкидається — лишається фіксований текст."""
    class _LLM:
        async def text(self, *a, **k):
            return "Красунчик! 40/37 зараховано!"
    flow, sender, _ = make_flow(tmp_path, llm=None)
    flow.llm = _LLM()
    fixed = DEFAULT_BOT_TEXTS["success"].format(reps=38, target=37, exercise="віджимань")
    flow._call = lambda fn, *a: fn(*a)
    assert await flow._maybe_paraphrase_video_reply(fixed) == fixed
