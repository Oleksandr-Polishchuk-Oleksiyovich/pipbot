"""
Тести пробудження box (BoxWaker, gateway endpoints, інтеграція з ChatFlow).
"""
import asyncio
import http.server
import json
import logging
import os
import threading
from typing import Optional

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.flow import ChatFlow
from core.signing import sign_request
from core.wake import BoxWaker, load_wake_config
from database.store import Store
from gateway.app import create_app
from tests.flow_fixtures import CHAT, NOW, Clock, FakeSender

SECRET = "test-secret"


class FakeWebhookHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress stderr logging in tests

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(length) if length > 0 else b""
        try:
            body_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else None
        except Exception:
            body_json = None

        server = self.server
        with server.lock:
            server.requests.append({
                "headers": dict(self.headers),
                "auth": self.headers.get("Authorization"),
                "body": body_json,
                "raw_body": body_bytes,
            })
            status = server.statuses.pop(0) if server.statuses else 200

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')


class FakeWebhookServer:
    def __init__(self, statuses=None):
        self.statuses = list(statuses or [200])
        self.requests = []
        self.lock = threading.Lock()
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), FakeWebhookHandler)
        self.httpd.lock = self.lock
        self.httpd.requests = self.requests
        self.httpd.statuses = self.statuses
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/webhook"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def set_statuses(self, statuses):
        with self.lock:
            self.statuses.clear()
            self.statuses.extend(statuses)

    @property
    def request_count(self):
        with self.lock:
            return len(self.requests)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def fake_webhook_server():
    servers = []

    def _create(statuses=None):
        srv = FakeWebhookServer(statuses)
        servers.append(srv)
        return srv

    yield _create

    for srv in servers:
        try:
            srv.close()
        except Exception:
            pass


async def _create_gw_client(tmp_path, store=None):
    if store is None:
        store = Store(str(tmp_path / "gw.db"))
        store.init_schema()
    app = create_app(
        store=store,
        secret=SECRET,
        cv_options={"claim_wait_s": 0.1, "lease_s": 300, "video_dir": str(tmp_path / "vids")},
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, app


# 1. retries_on_5xx_then_success
@pytest.mark.asyncio
async def test_retries_on_5xx_then_success(tmp_path, fake_webhook_server):
    srv = fake_webhook_server([500, 503, 200])
    store = Store(str(tmp_path / "test1.db"))
    store.init_schema()
    sleep_delays = []

    async def fake_sleep(d):
        sleep_delays.append(d)

    waker = BoxWaker(
        store,
        attempts=3,
        backoff=(2.0, 5.0, 10.0),
        get_config=lambda: {"url": srv.url, "header": "Authorization", "value": "Bearer k"},
        sleep=fake_sleep,
    )
    res = await waker.fire("test_reason")
    assert res is True
    assert srv.request_count == 3
    assert sleep_delays == [2.0, 5.0]


# 2. all_attempts_fail
@pytest.mark.asyncio
async def test_all_attempts_fail(tmp_path, fake_webhook_server):
    srv = fake_webhook_server([500, 500, 500])
    store = Store(str(tmp_path / "test2.db"))
    store.init_schema()

    async def fake_sleep(d):
        pass

    waker = BoxWaker(
        store,
        attempts=3,
        get_config=lambda: {"url": srv.url, "header": "Authorization", "value": "Bearer k"},
        sleep=fake_sleep,
    )
    res = await waker.fire("test_reason")
    assert res is False
    assert srv.request_count == 3


# 3. any_2xx_counts
@pytest.mark.asyncio
async def test_any_2xx_counts(tmp_path, fake_webhook_server):
    srv = fake_webhook_server([202])
    store = Store(str(tmp_path / "test3.db"))
    store.init_schema()

    async def fake_sleep(d):
        pass

    waker = BoxWaker(
        store,
        attempts=3,
        get_config=lambda: {"url": srv.url, "header": "Authorization", "value": "Bearer k"},
        sleep=fake_sleep,
    )
    res = await waker.fire("test_reason")
    assert res is True
    assert srv.request_count == 1
    req = srv.requests[0]
    assert req["auth"] == "Bearer k"
    assert "key" not in req["body"]
    assert "Bearer" not in json.dumps(req["body"])


# 4. stale_detection
def test_stale_detection(tmp_path):
    curr_time = 1000.0

    def clock():
        return curr_time

    store = Store(str(tmp_path / "test4.db"))
    store.init_schema()
    waker = BoxWaker(store, stale_s=45.0, lease_s=300.0, clock=clock)

    # без cv_last_poll_ts → worker_alive() False
    assert waker.worker_alive() is False

    # після store.set_state("cv_last_poll_ts", str(clock())) → True
    store.set_state("cv_last_poll_ts", str(clock()))
    assert waker.worker_alive() is True

    # через stale_s+1 с (clock++) → False
    curr_time += 46.0
    assert waker.worker_alive() is False

    # але з claimed job у межах lease (store.enqueue_cv_job + store.claim_cv_job) → True
    store.enqueue_cv_job("r1")
    claimed = store.claim_cv_job(lease_seconds=300)
    assert claimed is not None
    assert waker.worker_alive() is True


# 5. alive_no_webhook
@pytest.mark.asyncio
async def test_alive_no_webhook(tmp_path, fake_webhook_server):
    srv = fake_webhook_server([200])
    store = Store(str(tmp_path / "test5.db"))
    store.init_schema()
    curr_time = 1000.0

    def clock():
        return curr_time

    store.set_state("cv_last_poll_ts", str(clock()))
    waker = BoxWaker(
        store,
        stale_s=45.0,
        get_config=lambda: {"url": srv.url, "header": "Authorization", "value": "Bearer k"},
        clock=clock,
    )
    res = await waker.ensure_awake()
    assert res == "alive"
    assert srv.request_count == 0


# 6. woken_after_webhook
@pytest.mark.asyncio
async def test_woken_after_webhook(tmp_path, fake_webhook_server):
    srv = fake_webhook_server([200])
    store = Store(str(tmp_path / "test6.db"))
    store.init_schema()
    curr_time = 1000.0

    def clock():
        return curr_time

    async def waking_sleep(delay):
        store.set_state("cv_last_poll_ts", str(clock()))

    waker = BoxWaker(
        store,
        stale_s=45.0,
        get_config=lambda: {"url": srv.url, "header": "Authorization", "value": "Bearer k"},
        sleep=waking_sleep,
        clock=clock,
    )
    res = await waker.ensure_awake()
    assert res == "woken"
    assert srv.request_count == 1


# 7. timeout_refires_and_notifies
@pytest.mark.asyncio
async def test_timeout_refires_and_notifies(tmp_path, fake_webhook_server):
    srv = fake_webhook_server([200, 200, 200, 200])
    clock = Clock(NOW)
    sender = FakeSender()
    store = Store(str(tmp_path / "flow.db"))

    async def fake_sleep(d):
        pass

    curr_time = 1000.0

    def waker_clock():
        return curr_time

    waker = BoxWaker(
        store,
        rounds=2,
        attempts=1,
        wait_s=0.0,
        get_config=lambda: {"url": srv.url, "header": "Authorization", "value": "Bearer k"},
        sleep=fake_sleep,
        clock=waker_clock,
    )

    results = []
    orig_ensure = waker.ensure_awake

    async def spy_ensure(*args, **kwargs):
        res = await orig_ensure(*args, **kwargs)
        results.append(res)
        return res

    waker.ensure_awake = spy_ensure

    flow = ChatFlow(
        store,
        None,
        sender,
        now=clock,
        obey_media_dir=str(tmp_path / "none"),
        trust_mode=False,
        waker=waker,
    )
    flow.init_storage()
    flow.cv_offload = True
    user = flow.store.get_user("lyosha")

    # Перше відео
    ok1, reply1 = await flow.handle_video(
        CHAT, user, message_id=10, duration=30.0, file_id="f10", file_unique_id="u10"
    )
    assert ok1 is True
    await flow.wait_box_wake()

    # воркер ніколи не оживає, rounds=2, attempts=1 → "timeout", 2 запити до вебхука
    assert results == ["timeout"]
    assert srv.request_count == 2
    # у FakeSender.texts є РІВНО одне повідомлення з "прокидається"
    waking_msgs = [t for t in sender.texts if "прокидається" in t]
    assert len(waking_msgs) == 1

    # Друге відео у тому ж епізоді (інший file_unique_id)
    ok2, reply2 = await flow.handle_video(
        CHAT, user, message_id=11, duration=30.0, file_id="f11", file_unique_id="u11"
    )
    assert ok2 is True
    await flow.wait_box_wake()

    # повідомлення вдруге НЕ шлеться
    waking_msgs_after = [t for t in sender.texts if "прокидається" in t]
    assert len(waking_msgs_after) == 1

    # job-и обох відео лишаються status 'queued' у cv_jobs (store.get_cv_job)
    checks = flow.store.list_video_checks()
    assert len(checks) == 2
    for c in checks:
        job = flow.store.get_cv_job(c.request_id)
        assert job is not None
        assert job["status"] == "queued"


# 8. no_secret_noop
@pytest.mark.asyncio
async def test_no_secret_noop(tmp_path, caplog):
    store = Store(str(tmp_path / "test8.db"))
    store.init_schema()
    posted = []

    def fake_poster(*args):
        posted.append(args)
        return 200

    waker = BoxWaker(store, get_config=lambda: None, poster=fake_poster)
    caplog.set_level(logging.WARNING, logger="wake")

    assert await waker.ensure_awake() == "disabled"
    assert len(posted) == 0

    assert await waker.watchdog() == "disabled"
    assert len(posted) == 0

    warn_records = [r for r in caplog.records if "not configured" in r.message.lower()]
    assert len(warn_records) == 1


# 9. disabled_flag
@pytest.mark.asyncio
async def test_disabled_flag(tmp_path):
    store = Store(str(tmp_path / "test9.db"))
    store.init_schema()
    posted = []

    def fake_poster(*args):
        posted.append(args)
        return 200

    waker = BoxWaker(
        store,
        enabled=False,
        get_config=lambda: {"url": "http://x", "header": "h", "value": "v"},
        poster=fake_poster,
    )
    assert await waker.ensure_awake() == "disabled"
    assert await waker.watchdog() == "disabled"
    assert len(posted) == 0


# 10. watchdog
@pytest.mark.asyncio
async def test_watchdog(tmp_path, fake_webhook_server):
    srv = fake_webhook_server([200, 200, 200])
    store = Store(str(tmp_path / "test10.db"))
    store.init_schema()
    curr_time = 1000.0

    def clock():
        return curr_time

    async def fake_sleep(d):
        pass

    waker = BoxWaker(
        store,
        stale_s=45.0,
        watchdog_interval_s=300.0,
        get_config=lambda: {"url": srv.url, "header": "Authorization", "value": "Bearer k"},
        sleep=fake_sleep,
        clock=clock,
    )

    # queued job старший за stale_s, воркер мовчить → watchdog() == "fired"
    store.enqueue_cv_job("job1")
    with store._connect() as con:
        con.execute(
            "UPDATE cv_jobs SET created_at = datetime('now', '-120 seconds') WHERE request_id = 'job1'"
        )
    assert await waker.watchdog() == "fired"

    # одразу вдруге → "rate_limited"
    assert await waker.watchdog() == "rate_limited"

    # після clock += watchdog_interval_s+1 → "fired"
    curr_time += waker.watchdog_interval_s + 1
    assert await waker.watchdog() == "fired"

    # порожня черга → "idle"
    with store._connect() as con:
        con.execute("DELETE FROM cv_jobs")
    assert await waker.watchdog() == "idle"

    # свіжий poll → "alive"
    store.enqueue_cv_job("job2")
    with store._connect() as con:
        con.execute(
            "UPDATE cv_jobs SET created_at = datetime('now', '-120 seconds') WHERE request_id = 'job2'"
        )
    store.set_state("cv_last_poll_ts", str(clock()))
    assert await waker.watchdog() == "alive"


# 11. gateway
@pytest.mark.asyncio
async def test_gateway_worker_status(tmp_path):
    store = Store(str(tmp_path / "gw.db"))
    store.init_schema()
    client, app = await _create_gw_client(tmp_path, store)
    try:
        # без підпису → 401
        r_unauth = await client.request("GET", "/cv/worker_status")
        assert r_unauth.status == 401

        # GET /cv/worker_status (підписаний HMAC) повертає queued/claimed/last_poll_age_s/worker_polling
        h = sign_request("GET", "/cv/worker_status", b"", SECRET)
        r = await client.request("GET", "/cv/worker_status", headers=h)
        assert r.status == 200
        data = await r.json()
        assert "queued" in data and data["queued"] == 0
        assert "claimed" in data and data["claimed"] == 0
        assert "last_poll_age_s" in data and data["last_poll_age_s"] is None
        assert "worker_polling" in data and data["worker_polling"] is False

        # після POST /cv/claim з порожньою чергою (wait=0) last_poll_age_s малий (<5) і worker_polling True
        h_claim = sign_request("POST", "/cv/claim", b"", SECRET)
        r_claim = await client.request("POST", "/cv/claim?wait=0", data=b"", headers=h_claim)
        assert r_claim.status == 204

        h_status = sign_request("GET", "/cv/worker_status", b"", SECRET)
        r_status = await client.request("GET", "/cv/worker_status", headers=h_status)
        assert r_status.status == 200
        data_status = await r_status.json()
        assert data_status["last_poll_age_s"] is not None
        assert data_status["last_poll_age_s"] < 5
        assert data_status["worker_polling"] is True
    finally:
        await client.close()


# 12. gateway secret endpoint
@pytest.mark.asyncio
async def test_gateway_secret_endpoint(tmp_path, monkeypatch, caplog):
    secret_file = tmp_path / "wake_secret.json"
    monkeypatch.setenv("WAKE_SECRET_FILE", str(secret_file))
    monkeypatch.delenv("WAKE_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("WAKE_WEBHOOK_KEY", raising=False)

    store = Store(str(tmp_path / "gw_secret.db"))
    store.init_schema()
    client, app = await _create_gw_client(tmp_path, store)
    try:
        valid_payload = json.dumps({"url": "https://example.invalid/hook", "key": "K"}).encode("utf-8")

        # без підпису → 401
        r_noauth = await client.request("POST", "/admin/secrets/wake", data=valid_payload)
        assert r_noauth.status == 401

        # http:// URL → 400
        http_payload = json.dumps({"url": "http://example.invalid/hook", "key": "K"}).encode("utf-8")
        h_http = sign_request("POST", "/admin/secrets/wake", http_payload, SECRET)
        r_http = await client.request("POST", "/admin/secrets/wake", data=http_payload, headers=h_http)
        assert r_http.status == 400

        # POST /admin/secrets/wake з {"url":"https://example.invalid/hook","key":"K"} підписаним запитом → 200 {"status":"stored"}
        caplog.set_level(logging.DEBUG)
        caplog.clear()
        h_valid = sign_request("POST", "/admin/secrets/wake", valid_payload, SECRET)
        r_valid = await client.request("POST", "/admin/secrets/wake", data=valid_payload, headers=h_valid)
        assert r_valid.status == 200
        assert await r_valid.json() == {"status": "stored"}

        # відповідь і caplog НЕ містять "K"
        resp_text = await r_valid.text()
        assert "K" not in resp_text
        assert not any("K" in rec.message for rec in caplog.records)

        # файл має mode 0o600 і містить url/key
        assert secret_file.exists()
        assert secret_file.stat().st_mode & 0o777 == 0o600
        with open(secret_file, "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert saved == {"url": "https://example.invalid/hook", "key": "K"}

        # load_wake_config() після цього повертає конфіг
        cfg = load_wake_config()
        assert cfg is not None
        assert cfg["url"] == "https://example.invalid/hook"
        assert cfg["header"] == "Authorization"
        assert cfg["value"] == "Bearer K"

        # GET /admin/secrets/wake без підпису → 401
        r_get_noauth = await client.request("GET", "/admin/secrets/wake")
        assert r_get_noauth.status == 401

        # GET /admin/secrets/wake підписаний → {"configured": true}
        h_get = sign_request("GET", "/admin/secrets/wake", b"", SECRET)
        r_get = await client.request("GET", "/admin/secrets/wake", headers=h_get)
        assert r_get.status == 200
        assert await r_get.json() == {"configured": True}
    finally:
        await client.close()


# 13. handle_video без waker (waker=None) або з cv_offload
@pytest.mark.asyncio
async def test_handle_video_no_waker(tmp_path):
    clock = Clock(NOW)
    sender = FakeSender()
    store = Store(str(tmp_path / "flow.db"))
    flow = ChatFlow(
        store,
        None,
        sender,
        now=clock,
        obey_media_dir=str(tmp_path / "none"),
        trust_mode=False,
        waker=None,
    )
    flow.init_storage()
    flow.cv_offload = True
    user = flow.store.get_user("lyosha")

    ok, reply = await flow.handle_video(
        CHAT, user, message_id=80, duration=35.0, file_id="f80", file_unique_id="u80"
    )
    assert ok is True
    assert reply == flow.bot_texts.get("processing")
    await flow.wait_box_wake()

    # job queued, жодних додаткових повідомлень окрім processing
    assert sender.texts == [flow.bot_texts.get("processing")]
    checks = flow.store.list_video_checks()
    assert len(checks) == 1
    job = flow.store.get_cv_job(checks[0].request_id)
    assert job is not None
    assert job["status"] == "queued"
