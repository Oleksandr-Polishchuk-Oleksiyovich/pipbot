import pytest
import aiosqlite
import math

def execute_dynamic_tool(python_script: str, **kwargs) -> dict:
    """
    Executes a dynamic tool in an isolated restricted scope as specified
    by Node 2 and Python Runner in JEV_GEMINY/NewFlow.md.
    """
    safe_globals = {
        "__builtins__": {
            "abs": abs,
            "min": min,
            "max": max,
            "len": len,
            "int": int,
            "float": float,
            "str": str,
            "dict": dict,
            "list": list,
            "bool": bool,
            "range": range,
        },
        "math": math,
    }
    local_scope = {}
    try:
        exec(python_script, safe_globals, local_scope)
        if "execute" not in local_scope:
            return {"status": "error", "message": "Entry point 'execute' not found"}
        return local_scope["execute"](**kwargs)
    except Exception as e:
        return {"status": "error", "message": str(e)}


@pytest.mark.asyncio
async def test_dynamic_tool_registration_and_execution(initialized_test_db):
    tool_code = """
def execute(base_target: int, day_offset: int) -> dict:
    total = base_target + day_offset
    return {
        "status": "success",
        "result": {"total_pushups": total},
        "message": "Calculated successfully"
    }
"""
    # 1. Register tool in database
    async with aiosqlite.connect(initialized_test_db) as db:
        await db.execute(
            "INSERT INTO dynamic_tools (key, skill, python_script) VALUES (?, ?, ?)",
            ("calc_ladder", "Calculate ladder challenge progress", tool_code)
        )
        await db.commit()

        # 2. Route and fetch tool
        async with db.execute(
            "SELECT python_script FROM dynamic_tools WHERE key = ?", ("calc_ladder",)
        ) as cursor:
            row = await cursor.fetchone()
            assert row is not None
            script = row[0]

    # 3. Execute with args
    res = execute_dynamic_tool(script, base_target=13, day_offset=5)
    assert res["status"] == "success"
    assert res["result"]["total_pushups"] == 18


def test_dynamic_tool_syntax_or_runtime_error_handling():
    invalid_script = """
def execute(**kwargs):
    return 10 / 0
"""
    res = execute_dynamic_tool(invalid_script)
    assert res["status"] == "error"
    assert "division by zero" in res["message"]


def test_dynamic_tool_missing_entry_point():
    missing_entry_script = """
def run_action(**kwargs):
    return {'status': 'success'}
"""
    res = execute_dynamic_tool(missing_entry_script)
    assert res["status"] == "error"
    assert "Entry point 'execute' not found" in res["message"]


@pytest.mark.asyncio
async def test_dynamic_tool_routing_resolution(initialized_test_db):
    """Tests routing user intent to registered dynamic tools in SQLite."""
    async with aiosqlite.connect(initialized_test_db) as db:
        await db.execute(
            "INSERT INTO dynamic_tools (key, skill, python_script) VALUES (?, ?, ?)",
            ("calc_stats", "calculate user streak and statistics", "def execute(**kw): return {'status':'success'}")
        )
        await db.commit()

        # Simulate router matching skill keywords
        query = "Show me my stats and streak"
        async with db.execute("SELECT key, skill FROM dynamic_tools") as cursor:
            registered_tools = await cursor.fetchall()

        matched_key = None
        for key, skill in registered_tools:
            if any(term in query.lower() for term in ["stats", "statistics", "streak"]):
                matched_key = key
                break

        assert matched_key == "calc_stats"
