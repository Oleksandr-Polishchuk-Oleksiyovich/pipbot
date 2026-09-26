import os
import sys
import tempfile
import pytest
import pytest_asyncio
import aiosqlite

# Ensure workspace root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

@pytest.fixture
def temp_db_path(tmp_path):
    """Provides a path to an isolated temporary SQLite database file."""
    db_file = tmp_path / "test_challenge.db"
    return str(db_file)

@pytest_asyncio.fixture
async def initialized_test_db(temp_db_path, monkeypatch):
    """Initializes the SQLite schema in an isolated test database."""
    import core.config
    import database.db_manager

    monkeypatch.setattr(core.config, "DB_PATH", temp_db_path)
    monkeypatch.setattr(database.db_manager, "DB_PATH", temp_db_path)

    await database.db_manager.init_db()
    yield temp_db_path
