"""admin_tasks: store, migration 0010, gateway endpoints (HMAC), CLI signing, post-deploy announcements."""
import importlib.util
import json
import sqlite3

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.flow import commit_is_deployed, load_release_commits
from core.signing import sign_request
from database.store import Store
from gateway.app import create_app

SECRET = "test-secret"


def _store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.init_schema()
    return s


def test_store_lifecycle(tmp_path):
    s = _store(tmp_path)
    tid = s.create_admin_task(-1, 42, "Саша", "додай рейтинг")
    assert s.get_admin_task(tid)["status"] == "pending"
    t = s.claim_admin_task()
    assert t["id"] == tid and t["status"] == "claimed"
    assert s.claim_admin_task() is None
    done = s.set_admin_task_result(tid, "done_pending_deploy", "abcdef1234", "ok")
    assert done["status"] == "done_pending_deploy" and done["fix_commit"] == "abcdef1234"
    assert s.set_admin_task_result(tid, "rejected") is None  # already resolved
    with pytest.raises(ValueError):
        s.set_admin_task_result(tid, "announced")
    s.mark_admin_task_announced(tid)
    assert s.get_admin_task(tid)["status"] == "announced"


def test_migration_0010_creates_table_and_seeds_texts(tmp_path):
    spec = importlib.util.spec_from_file_location("m10", "migrations/0010_admin_tasks.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    con = sqlite3.connect(str(tmp_path / "m.db"))
    con.execute("CREATE TABLE bot_texts (key TEXT PRIMARY KEY, text TEXT NOT NULL, variants TEXT, updated_by TEXT, updated_at TIMESTAMP)")
    con.execute("INSERT INTO bot_texts (key, text) VALUES ('admin_task_patched', 'custom')")
    m.upgrade(con)
    m.upgrade(con)  # idempotent
    keys = dict(con.execute("SELECT key, text FROM bot_texts").fetchall())
    assert keys["admin_task_patched"] == "custom"  # admin edit preserved
    assert "{id}" in keys["admin_task_created"]
    con.execute("INSERT INTO admin_tasks (text) VALUES ('x')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO admin_tasks (text, status) VALUES ('x', 'bogus')")


async def _client(store):
    app = create_app(store=store, secret=SECRET, cv_options={"claim_wait_s": 0.3})
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _req(client, method, path, body=b"", query=""):
    return await client.request(method, path + query, data=body or None, headers=sign_request(method, path, body, SECRET))


@pytest.mark.asyncio
async def test_gateway_claim_list_result(tmp_path):
    s = _store(tmp_path)
    client = await _client(s)
    try:
        r = await client.get("/admin_tasks/claim")
        assert r.status == 401
        r = await _req(client, "GET", "/admin_tasks/claim")
        assert r.status == 204
        tid = s.create_admin_task(-1, None, "Льоша", "зроби бонуси")
        r = await _req(client, "GET", "/admin_tasks/claim")
        assert r.status == 200 and (await r.json())["task"]["id"] == tid
        r = await _req(client, "GET", "/admin_tasks", query="?status=claimed")
        assert [t["id"] for t in (await r.json())["tasks"]] == [tid]
        body = json.dumps({"status": "done_pending_deploy"}).encode()
        r = await _req(client, "POST", f"/admin_tasks/{tid}/result", body)
        assert r.status == 400  # fix_commit required
        body = json.dumps({"status": "done_pending_deploy", "fix_commit": "1234567abc", "result": "ok"}).encode()
        r = await _req(client, "POST", f"/admin_tasks/{tid}/result", body)
        assert r.status == 200
        r = await _req(client, "POST", f"/admin_tasks/{tid}/result", body)
        assert r.status == 409
    finally:
        await client.close()


def test_cli_signs_path_without_query(monkeypatch):
    from core.signing import verify_request
    from tools import admin_tasks as cli
    captured = {}

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"tasks": []}'

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        return Resp()

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)
    assert cli.AdminTasksClient("http://gw", SECRET).list("pending") == []
    assert captured["url"] == "http://gw/admin_tasks?status=pending"
    hdrs = {k.lower(): v for k, v in captured["headers"].items()}

    class H(dict):
        def get(self, k, d=None): return super().get(k.lower(), d)
    ok, reason = verify_request("GET", "/admin_tasks", b"", H(hdrs), SECRET)
    assert ok, reason


def test_release_commit_matching(tmp_path):
    assert load_release_commits(str(tmp_path)) is None
    assert commit_is_deployed("abc1234", None) is True
    (tmp_path / "REVISION").write_text("a" * 40 + "\n")
    (tmp_path / "REVISION_COMMITS").write_text("a" * 40 + "\n" + "b" * 40 + "\n")
    dep = load_release_commits(str(tmp_path))
    assert commit_is_deployed("bbbbbbb", dep)
    assert not commit_is_deployed("ccccccc", dep)
    assert not commit_is_deployed("", dep)


@pytest.mark.asyncio
async def test_announcements(tmp_path):
    from tests.flow_fixtures import CHAT, make_flow
    flow, sender, _ = make_flow(tmp_path, llm=None)
    rel = tmp_path / "rel"
    rel.mkdir()
    (rel / "REVISION").write_text("a" * 40 + "\n")
    s = flow.store
    t1 = s.create_admin_task(CHAT, None, "Саша", "рейтинг")
    t2 = s.create_admin_task(CHAT, None, "Льоша", "не в релізі")
    t3 = s.create_admin_task(CHAT, None, "Діма", "погане")
    s.set_admin_task_result(t1, "done_pending_deploy", "a" * 12)
    s.set_admin_task_result(t2, "done_pending_deploy", "c" * 12)
    s.set_admin_task_result(t3, "rejected", None, "не по темі")

    # тік (не старт): лише відмова
    assert await flow.announce_admin_tasks(CHAT, release_dir=str(rel)) == 1
    assert any("відхилено" in t and "не по темі" in t for t in sender.texts)
    assert await flow.announce_admin_tasks(CHAT, release_dir=str(rel)) == 0

    assert await flow.announce_admin_tasks(CHAT, startup=True, release_dir=str(rel)) == 1
    assert any("Пропатчився на запит Саша" in t and "рейтинг" in t for t in sender.texts)
    assert s.get_admin_task(t1)["status"] == "announced"
    assert s.get_admin_task(t2)["status"] == "done_pending_deploy"
    assert await flow.announce_admin_tasks(CHAT, startup=True, release_dir=str(rel)) == 0


def test_create_admin_task_tool(tmp_path):
    from datetime import date
    from agents.admin import AdminAgent
    from tests.flow_fixtures import make_flow
    flow, _, _ = make_flow(tmp_path, llm=None)
    agent = AdminAgent(None, flow.store, editor=None)
    actions = []
    tools = {t.__name__: t for t in agent._tools(date(2026, 10, 21), "Саша", actions)}
    reply = tools["create_admin_task"]("додати таблицю рекордів")
    task = flow.store.list_admin_tasks(status="pending")[0]
    assert f"#{task['id']}" in reply and task["requester_name"] == "Саша"
    assert actions == [f"create_admin_task(#{task['id']})"]
