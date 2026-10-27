"""Діагностика VM (/admin/diag) та одноразові ідемпотентні адмін-операції (/admin/ops), день відео після rollover."""
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from core.signing import sign_request
from database.store import Store
from gateway import ops_api
from gateway.app import create_app
from tests.flow_fixtures import CHAT, make_flow

SECRET = "test-secret"


async def _client(tmp_path):
    store = Store(str(tmp_path / "ops.db"))
    store.init_schema()
    app = create_app(store=store, secret=SECRET, cv_options={"claim_wait_s": 0.1, "lease_s": 300, "video_dir": str(tmp_path / "v")})
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, store


def _user(store, slug="dima", lives=2, max_lives=3):
    from database.store import User
    store.upsert_user(User(slug=slug, telegram_id=111, telegram_name="Dmytro", username="dima_k", display_name="Діма",
                           emoji="💪", status="active", lives=lives, max_lives=max_lives, joined_date="2026-08-31"))


async def _ops(client, **body):
    raw = json.dumps(body, ensure_ascii=False).encode()
    h = sign_request("POST", "/admin/ops", raw, SECRET)
    h["Content-Type"] = "application/json"
    r = await client.post("/admin/ops", data=raw, headers=h)
    return r.status, await r.json()


def test_redact_hides_tokens():
    s = ops_api.redact("url=https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw9/x Bearer abc.def key=SECRET123")
    assert "AAHdqT" not in s and "SECRET123" not in s and "abc.def" not in s


@pytest.mark.asyncio
async def test_diag_requires_auth_and_lists_state(tmp_path):
    client, store = await _client(tmp_path)
    try:
        _user(store)
        assert (await client.get("/admin/diag")).status == 401
        h = sign_request("GET", "/admin/diag", b"", SECRET)
        r = await client.get("/admin/diag?log_lines=0", headers=h)
        assert r.status == 200
        d = await r.json()
        assert d["users"][0]["slug"] == "dima" and "cv_jobs_by_status" in d
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_adjust_user_idempotent_caps_lives_and_levels(tmp_path):
    client, store = await _client(tmp_path)
    try:
        _user(store, lives=2, max_lives=3)
        st, out = await _ops(client, op="adjust_user", key="t-adj-1", params={"slug": "dima", "lives_delta": 1, "xp_delta": 10, "date": "2026-10-25"})
        assert st == 200 and out["status"] == "done"
        a = out["record"]["result"]["after"]
        assert (a["lives"], a["xp"]) == (3, 10)
        st, out = await _ops(client, op="adjust_user", key="t-adj-1", params={"slug": "dima", "lives_delta": 1, "xp_delta": 10})
        assert out["status"] == "already_done"
        assert store.get_xp("dima")[0] == 10 and store.get_user("dima").lives == 3
        # кап: ще +1 життя з іншим ключем не перевищує max
        _, out = await _ops(client, op="adjust_user", key="t-adj-2", params={"slug": "dima", "lives_delta": 1})
        assert out["record"]["result"]["after"]["lives"] == 3
        # рівень: +95 XP => 105 -> рівень 1, +1 max життя, життя до максимуму
        _, out = await _ops(client, op="adjust_user", key="t-adj-3", params={"slug": "dima", "xp_delta": 95})
        a = out["record"]["result"]["after"]
        assert (a["xp"], a["level"], a["max_lives"], a["lives"]) == (5, 1, 4, 4)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_credit_attempt_dates_and_idempotent(tmp_path):
    client, store = await _client(tmp_path)
    try:
        _user(store)
        from datetime import date
        from core import challenge
        ex = challenge.day_exercise(store, date(2026, 10, 25))
        st, out = await _ops(client, op="credit_attempt", key="t-cr-1", params={"slug": "dima", "date": "2026-10-25", "exercise": ex, "reps": 5})
        assert st == 200
        assert store.get_best_attempt("2026-10-25", "dima") == 5 and store.get_best_attempt("2026-10-26", "dima") is None
        _, again = await _ops(client, op="credit_attempt", key="t-cr-1", params={"slug": "dima", "date": "2026-10-25", "exercise": ex, "reps": 99})
        assert again["status"] == "already_done" and store.get_best_attempt("2026-10-25", "dima") == 5
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_credit_attempt_sets_video_credit_for_request(tmp_path):
    client, store = await _client(tmp_path)
    try:
        _user(store)
        from datetime import date
        from core import challenge
        ex = challenge.day_exercise(store, date(2026, 10, 25))
        store.create_video_check(request_id="rid-dima", chat_id=CHAT, message_id=1, user_slug="dima", status="done",
                                 exercise="squat", reps=3, valid=True)
        store.set_state("video_credit:rid-dima", json.dumps({"reps": 3, "ex": ex}))
        params = {"slug": "dima", "date": "2026-10-25", "exercise": ex, "reps": 7, "request_id": "rid-dima"}
        st, out = await _ops(client, op="credit_attempt", key="t-cr-rid", params=params)
        assert st == 200 and out["record"]["result"]["video_credit"]["reps"] == 7
        assert json.loads(store.get_state("video_credit:rid-dima", "")) == {"reps": 7, "ex": ex, "disputed": True}
        # чуже/невідоме відео відхиляється
        st, out = await _ops(client, op="credit_attempt", key="t-cr-rid2", params={**params, "request_id": "nope"})
        assert out.get("status") != "done"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_send_message_exactly_once(tmp_path, monkeypatch):
    client, store = await _client(tmp_path)
    sent = []
    monkeypatch.setattr(ops_api, "_telegram_send", lambda tok, chat, text, rt: sent.append((chat, text)) or {"ok": True, "result": {"message_id": 77}})
    monkeypatch.setattr("core.config.BOT_TOKEN", "x")
    try:
        _, o1 = await _ops(client, op="send_message", key="t-msg-1", params={"chat_id": CHAT, "text": "Привіт"})
        assert o1["record"]["result"]["message_id"] == 77
        _, o2 = await _ops(client, op="send_message", key="t-msg-1", params={"chat_id": CHAT, "text": "Привіт"})
        assert o2["status"] == "already_done" and len(sent) == 1
        # перерваний send (стан 'sending') не повторюється
        store.set_state("ops:t-msg-2", json.dumps({"op": "send_message", "status": "sending"}))
        _, o3 = await _ops(client, op="send_message", key="t-msg-2", params={"chat_id": CHAT, "text": "x"})
        assert o3["status"] == "uncertain" and len(sent) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_task_add_and_cleanup(tmp_path):
    client, store = await _client(tmp_path)
    try:
        _, o = await _ops(client, op="admin_task_add", key="t-task-1", params={"text": "верни Льоші 3 життя", "chat_id": CHAT,
                                                                                 "requester_telegram_id": 1, "requester_name": "O", "status": "claimed"})
        tid = o["record"]["result"]["task_id"]
        assert store.get_admin_task(tid)["status"] == "claimed"
        _, o = await _ops(client, op="admin_task_add", key="t-task-1", params={"text": "dup"})
        assert o["status"] == "already_done" and len(store.list_admin_tasks()) == 1
        store.enqueue_cv_job("old-rid")
        with store._connect() as con:
            con.execute("UPDATE cv_jobs SET created_at = datetime('now','-10 hours')")
        _, dry = await _ops(client, op="cv_cleanup", key="t-clean-1", params={}, dry_run=True)
        assert dry["result"]["stale_cv_jobs"] == ["old-rid"] and store.count_cv_jobs("queued") == 1
        _, real = await _ops(client, op="cv_cleanup", key="t-clean-2", params={})
        assert store.count_cv_jobs("queued") == 0 and store.count_cv_jobs("failed") == 1
        assert real["record"]["result"]["after"]["cv_jobs"] == {"failed": 1}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_video_after_rollover_is_credited_to_video_day(tmp_path):
    flow, sender, clock = make_flow(tmp_path, llm=None, trust_mode=True)
    clock.value = clock.value.replace(day=5, hour=3, minute=0)  # 05.10 03:00 — після rollover 02:30
    flow.store.create_video_check(request_id="req-late", chat_id=CHAT, message_id=5, user_slug="lyosha", duration_s=30.0,
                                  exercise="pushup", reps=40, confidence=0.9, valid=True, status="done", flags=[])
    with flow.store._connect() as con:
        con.execute("UPDATE video_checks SET created_at='2026-10-04 15:00:00' WHERE request_id='req-late'")  # 18:00 Kyiv, 04.10
    await flow.handle_video_callback("req-late")
    assert flow.store.get_best_attempt("2026-10-04", "lyosha") == 40
    assert flow.store.get_best_attempt("2026-10-05", "lyosha") is None


@pytest.mark.asyncio
async def test_wake_test_op_fires_once_and_hides_secret(tmp_path, monkeypatch):
    client, store = await _client(tmp_path)
    calls = []
    import core.wake as wake
    monkeypatch.setattr(wake, "load_wake_config", lambda path=None: {"url": "https://x.invalid/h", "header": "Authorization", "value": "Bearer SEKRET"})
    monkeypatch.setattr(wake, "post_webhook", lambda cfg, payload, timeout=20: calls.append(payload) or 202)
    monkeypatch.setattr(wake.BoxWaker, "__init__", _patched_init(wake.BoxWaker.__init__))
    try:
        st, out = await _ops(client, op="wake_test", key="t-wake-1", params={})
        assert st == 200 and out["record"]["result"]["accepted_2xx"] is True and len(calls) == 1
        assert "SEKRET" not in json.dumps(out)
        _, again = await _ops(client, op="wake_test", key="t-wake-1", params={})
        assert again["status"] == "already_done" and len(calls) == 1
    finally:
        await client.close()


def _patched_init(orig):
    import core.wake as wake

    def init(self, store, **kw):
        kw["get_config"] = wake.load_wake_config
        kw["poster"] = wake.post_webhook
        orig(self, store, **kw)
    return init


@pytest.mark.asyncio
async def test_leave_ops_idempotent(tmp_path):
    client, store = await _client(tmp_path)
    try:
        _user(store, slug="dima")
        # 1. set_leave
        st, out = await _ops(
            client,
            op="set_leave",
            key="t-leave-1",
            params={
                "slug": "dima",
                "kind": "sick",
                "start_date": "2026-10-26",
                "end_date": "2026-10-30",
                "set_by": "tester",
                "reason": "cold",
            },
        )
        assert st == 200 and out["status"] == "done"
        rec = out["record"]["result"]["leave"]
        assert rec["kind"] == "sick"
        assert rec["start_date"] == "2026-10-26"
        assert rec["end_date"] == "2026-10-30"

        # Ідемпотентність: повторний виклик з тим же key
        _, again = await _ops(
            client,
            op="set_leave",
            key="t-leave-1",
            params={"slug": "dima", "kind": "sick"},
        )
        assert again["status"] == "already_done"

        # Перевіряємо в БД
        assert store.get_leave_on("dima", "2026-10-27") is not None

        # 2. end_leave
        st, out_end = await _ops(
            client,
            op="end_leave",
            key="t-leave-end-1",
            params={"slug": "dima", "from_day": "2026-10-28", "ended_by": "tester"},
        )
        assert st == 200 and out_end["status"] == "done"
        end_rec = out_end["record"]["result"]["leave"]
        assert end_rec["end_date"] == "2026-10-27"
        assert store.get_leave_on("dima", "2026-10-28") is None
        assert store.get_leave_on("dima", "2026-10-27") is not None

        # Ідемпотентність end_leave
        _, again_end = await _ops(
            client,
            op="end_leave",
            key="t-leave-end-1",
            params={"slug": "dima", "from_day": "2026-10-28"},
        )
        assert again_end["status"] == "already_done"
    finally:
        await client.close()


def _dima_settled_state(store, lives=7, max_lives=7, xp=2, level=2):
    """Стан Діми після розрахунку 26.10: best=50 (замість 23), +89 XP, левел-ап 1->2, next pushups 29."""
    _user(store, slug="kovdimay", lives=lives, max_lives=max_lives)
    store.set_attempt("2026-10-26", "kovdimay", "pushups", 50)
    store.set_done("kovdimay", True, "2026-10-26")
    store.set_xp("kovdimay", xp, level)
    store.add_xp_log("2026-10-26", "kovdimay", 89, "target=6 best=50 credited=True")
    store.set_next_norm("kovdimay", "pushups", 29, "2026-10-27", "2026-10-26")
    store.set_state("progression_settled_through", "2026-10-26")
    store.set_state("video_credit:rid-dima", json.dumps({"reps": 50, "ex": "pushups", "disputed": True}))


_FIX23 = {"slug": "kovdimay", "date": "2026-10-26", "exercise": "pushups", "reps": 23, "request_id": "rid-dima"}


@pytest.mark.asyncio
async def test_correct_settlement_dima_dry_run_then_apply_idempotent(tmp_path):
    client, store = await _client(tmp_path)
    try:
        _dima_settled_state(store)
        st, out = await _ops(client, op="correct_settlement", key="fix-dry", params=_FIX23, dry_run=True)
        assert st == 200 and out["status"] == "dry_run"
        plan = out["result"]
        assert plan["before"]["xp"] == 2 and plan["before"]["level"] == 2 and plan["before"]["next_norm"] == 29
        assert plan["after"] == {"attempt": 23, "done": True, "settle_xp_gain": 35, "xp": 48, "level": 1,
                                 "lives": 6, "max_lives": 6, "next_norm": 15}
        assert plan["xp_log_delta"] == -54
        assert store.get_best_attempt("2026-10-26", "kovdimay") == 50      # dry-run нічого не міняє
        assert store.get_xp("kovdimay") == (2, 2)

        st, out = await _ops(client, op="correct_settlement", key="ops-fix23", params=_FIX23)
        assert st == 200 and out["status"] == "done"
        assert store.get_best_attempt("2026-10-26", "kovdimay") == 23
        assert store.get_day_progress("2026-10-26")["kovdimay"] is True
        assert store.get_xp("kovdimay") == (48, 1)
        u = store.get_user("kovdimay")
        assert (u.lives, u.max_lives) == (6, 6)
        assert store.get_next_norm("kovdimay", "pushups") == (15, "2026-10-27")
        assert json.loads(store.get_state("video_credit:rid-dima", "")) == {"reps": 23, "ex": "pushups", "disputed": True}
        with store._connect() as con:
            rows = con.execute("SELECT delta, reason FROM xp_log WHERE user_slug='kovdimay' ORDER BY id").fetchall()
        assert [r[0] for r in rows] == [89, -54] and rows[1][1].startswith("correction-settlement")

        # той самий key -> already_done; інший key -> noop за станом (XP не зсувається вдруге)
        st, out = await _ops(client, op="correct_settlement", key="ops-fix23", params=_FIX23)
        assert out["status"] == "already_done"
        st, out = await _ops(client, op="correct_settlement", key="ops-fix23-again", params=_FIX23)
        assert out["status"] == "done" and out["record"]["result"]["noop"] is True
        assert store.get_xp("kovdimay") == (48, 1)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_correct_settlement_guards(tmp_path):
    client, store = await _client(tmp_path)
    try:
        _dima_settled_state(store)
        # день ще не розраховано -> помилка
        store.set_state("progression_settled_through", "2026-10-25")
        st, out = await _ops(client, op="correct_settlement", key="ops-g1", params=_FIX23)
        assert st == 422 and "not settled" in out["error"]
        store.set_state("progression_settled_through", "2026-10-26")
        # next_norms уже перезаписано пізнішим розрахунком -> не чіпаємо
        store.set_next_norm("kovdimay", "pushups", 40, "2026-10-29", "2026-10-28")
        st, out = await _ops(client, op="correct_settlement", key="ops-g2", params=_FIX23)
        assert out["status"] == "done" and out["record"]["result"]["next_norm_updated"] is False
        assert store.get_next_norm("kovdimay", "pushups") == (40, "2026-10-29")
        # погана вправа
        st, out = await _ops(client, op="correct_settlement", key="ops-g3", params={**_FIX23, "exercise": "планка"})
        assert st == 422
    finally:
        await client.close()
