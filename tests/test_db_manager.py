import pytest
import aiosqlite
import database.db_manager as db_manager
import core.config as config

@pytest.mark.asyncio
async def test_init_db_creates_all_required_tables(temp_db_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", temp_db_path)
    monkeypatch.setattr(db_manager, "DB_PATH", temp_db_path)

    await db_manager.init_db()

    async with aiosqlite.connect(temp_db_path) as db:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
        ) as cursor:
            tables = [row[0] for row in await cursor.fetchall()]

    expected_tables = ["dynamic_tools", "prompt_store", "system_config", "user_profiles"]
    for table in expected_tables:
        assert table in tables, f"Expected table '{table}' not found in SQLite database"


@pytest.mark.asyncio
async def test_init_db_is_idempotent(temp_db_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", temp_db_path)
    monkeypatch.setattr(db_manager, "DB_PATH", temp_db_path)

    # First initialization
    await db_manager.init_db()

    # Insert test data into prompt_store
    async with aiosqlite.connect(temp_db_path) as db:
        await db.execute(
            "INSERT INTO prompt_store (prompt_type, content) VALUES (?, ?)",
            ("current", "Sample system prompt")
        )
        await db.commit()

    # Re-running init_db must not drop existing data or fail
    await db_manager.init_db()

    async with aiosqlite.connect(temp_db_path) as db:
        async with db.execute("SELECT content FROM prompt_store WHERE prompt_type = 'current'") as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "Sample system prompt"


@pytest.mark.asyncio
async def test_prompt_store_unique_constraint(initialized_test_db):
    async with aiosqlite.connect(initialized_test_db) as db:
        await db.execute(
            "INSERT INTO prompt_store (prompt_type, content) VALUES (?, ?)",
            ("initial", "Base prompt")
        )
        await db.commit()

        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO prompt_store (prompt_type, content) VALUES (?, ?)",
                ("initial", "Duplicate prompt type")
            )
            await db.commit()


@pytest.mark.asyncio
async def test_user_profiles_default_json_arrays(initialized_test_db):
    async with aiosqlite.connect(initialized_test_db) as db:
        await db.execute(
            "INSERT INTO user_profiles (user_id, name) VALUES (?, ?)",
            ("user_123", "Test User")
        )
        await db.commit()

        async with db.execute(
            "SELECT user_id, name, likes, dislikes, interests FROM user_profiles WHERE user_id = ?",
            ("user_123",)
        ) as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "user_123"
            assert row[1] == "Test User"
            assert row[2] == "[]"
            assert row[3] == "[]"
            assert row[4] == "[]"


@pytest.mark.asyncio
async def test_dynamic_tools_crud(initialized_test_db):
    test_script = "def execute(**kwargs):\n    return {'status': 'success', 'result': 42, 'message': 'ok'}"
    async with aiosqlite.connect(initialized_test_db) as db:
        await db.execute(
            "INSERT INTO dynamic_tools (key, skill, python_script) VALUES (?, ?, ?)",
            ("calc_target", "Calculate daily pushup target", test_script)
        )
        await db.commit()

        async with db.execute(
            "SELECT key, skill, python_script FROM dynamic_tools WHERE key = ?",
            ("calc_target",)
        ) as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "calc_target"
            assert row[1] == "Calculate daily pushup target"
            assert "def execute" in row[2]
