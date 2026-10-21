"""Box CV offload: gateway /cv/* long-poll, lease expiry, HMAC/replay rejection, box worker loop."""
import asyncio
import json
import sqlite3
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from core.signing import sign_request
from cv_worker.box_worker import BACKOFF_STEPS, BoxCVWorker, heavy_cv_command
from database.store import Store
from gateway.app import create_app
from gateway.cv_queue import notify_job_enqueued

SECRET = "test-secret"


def _store(tmp_path):
    s = Store(str(tmp_path / "gw.db"))
    s.init_schema()
    return s


def _add_job(store, rid="r1"):
    store.create_video_check(request_id=rid, chat_id=-1, message_id=5, user_slug="lyosha",
                             file_id="fid_" + rid, status="offloaded", location="box")
    store.enqueue_cv_job(rid)


async def _client(tmp_path, store, wait=1.0, lease=300):
    def fetcher(file_id, dest):
        with open(dest, "wb") as f:
            f.write(b"VIDEO:" + file_id.encode())
    app = create_app(store=store, secret=SECRET, cv_options={
        "claim_wait_s": wait, "lease_s": lease, "video_dir": str(tmp_path / "vids"), "video_fetcher": fetcher})
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, app


async def _req(client, method, path, body=b"", secret=SECRET, headers=None):
    h = headers or sign_request(method, path, body, secret)
    return await client.request(method, path, data=body or None, headers=h)


@pytest.mark.asyncio
async def test_claim_timeout_returns_204(tmp_path):
    client, _ = await _client(tmp_path, _store(tmp_path), wait=0.6)
    try:
        t0 = time.monotonic()
        r = await _req(client, "POST", "/cv/claim", b"{}")
        assert r.status == 204
        assert 0.5 <= time.monotonic() - t0 < 2.0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_job_enqueued_during_open_claim_returns_fast(tmp_path):
    store = _store(tmp_path)
    client, app = await _client(tmp_path, store, wait=10)
    try:
        task = asyncio.create_task(_req(client, "POST", "/cv/claim", b"{}"))
        await asyncio.sleep(0.3)
        t0 = time.monotonic()
        _add_job(store, "r-late")        # як бот в іншому процесі (лише БД)
        r = await task
        assert r.status == 200
        assert time.monotonic() - t0 < 1.0
        job = await r.json()
        assert job["request_id"] == "r-late" and job["claim_token"]
        assert store.get_video_check("r-late").status == "running"

        # in-process Event теж будить long-poll
        task2 = asyncio.create_task(_req(client, "POST", "/cv/claim", b"{}"))
        await asyncio.sleep(0.2)
        _add_job(store, "r-evt")
        notify_job_enqueued(app)
        t1 = time.monotonic()
        r2 = await task2
        assert r2.status == 200 and time.monotonic() - t1 < 1.0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_full_cycle_video_and_result(tmp_path):
    store = _store(tmp_path)
    _add_job(store)
    client, _ = await _client(tmp_path, store)
    try:
        job = await (await _req(client, "POST", "/cv/claim", b"{}")).json()
        v = await _req(client, "GET", job["video_url"])
        assert v.status == 200 and await v.read() == b"VIDEO:fid_r1"

        body = json.dumps({"claim_token": job["claim_token"], "ok": True,
                           "result": {"exercise": "pushup", "reps": 22, "confidence": 0.55, "valid": True,
                                      "flags": ["low_camera_angle"]},
                           "timings": {"total_s": 9.1}}).encode()
        r = await _req(client, "POST", "/cv/jobs/r1/result", body)
        assert r.status == 200 and (await r.json())["status"] == "done"
        vc = store.get_video_check("r1")
        assert vc.status == "done" and vc.reps == 22 and "low_camera_angle" in vc.flags
        assert store.list_cv_jobs_to_reply()[0]["request_id"] == "r1"
        # повторний/застарілий результат відхиляється
        r_dup = await _req(client, "POST", "/cv/jobs/r1/result", body)
        assert r_dup.status == 409
        # відео прибрано після done
        assert (await _req(client, "GET", job["video_url"])).status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_hmac_reject_and_replay(tmp_path):
    client, _ = await _client(tmp_path, _store(tmp_path), wait=0.1)
    try:
        assert (await _req(client, "POST", "/cv/claim", b"{}", secret="wrong")).status == 401
        assert (await client.post("/cv/claim", data=b"{}")).status == 401
        old = sign_request("POST", "/cv/claim", b"{}", SECRET, ts=int(time.time()) - 3600)
        assert (await _req(client, "POST", "/cv/claim", b"{}", headers=old)).status == 401
        h = sign_request("POST", "/cv/claim", b"{}", SECRET)
        assert (await _req(client, "POST", "/cv/claim", b"{}", headers=h)).status == 204
        replay = await _req(client, "POST", "/cv/claim", b"{}", headers=h)
        assert replay.status == 401 and (await replay.json())["detail"] == "replayed_nonce"
        # підпис прив'язаний до path
        h2 = sign_request("POST", "/cv/claim", b"{}", SECRET)
        assert (await _req(client, "POST", "/cv/jobs/x/result", b"{}", headers=h2)).status == 401
    finally:
        await client.close()


def test_lease_expiry_requeues_then_fails(tmp_path):
    store = _store(tmp_path)
    _add_job(store, "r-lease")

    def expire():
        con = sqlite3.connect(store.db_path)
        con.execute("UPDATE cv_jobs SET claimed_at = datetime('now', '-10 minutes')")
        con.commit()
        con.close()

    j1 = store.claim_cv_job(lease_seconds=300)
    assert j1["attempts"] == 1 and store.claim_cv_job(lease_seconds=300) is None
    expire()
    j2 = store.claim_cv_job(lease_seconds=300)
    assert j2["request_id"] == "r-lease" and j2["attempts"] == 2 and j2["claim_token"] != j1["claim_token"]
    # старий токен більше не приймається
    assert store.complete_cv_job("r-lease", j1["claim_token"], ok=True) is None
    expire()
    assert store.claim_cv_job(lease_seconds=300)["attempts"] == 3
    expire()
    assert store.claim_cv_job(lease_seconds=300) is None
    assert store.get_cv_job("r-lease")["status"] == "failed"


@pytest.mark.asyncio
async def test_flow_replies_after_box_result_and_still_counting_once(tmp_path):
    from tests.flow_fixtures import CHAT, make_flow
    flow, sender, _ = make_flow(tmp_path, llm=None)
    user = flow.store.get_user("lyosha")
    ok, reply = await flow.handle_video(CHAT, user, message_id=70, duration=40.0, file_id="f70", file_unique_id="u70")
    assert ok and "попкорн" in reply.lower()
    rid = flow.store.list_video_checks(status="offloaded")[0].request_id

    await flow.process_cv_results(still_counting_min=0)
    await flow.process_cv_results(still_counting_min=0)
    still = [r for r in sender.replies if "ще рахую" in str(r).lower()]
    assert len(still) == 1

    job = flow.store.claim_cv_job()
    flow.store.complete_cv_job(rid, job["claim_token"], ok=True)
    flow.store.update_video_check(rid, status="done", exercise="pushup", reps=40, confidence=0.55,
                                  valid=True, flags=["low_camera_angle"], finished=True)
    assert await flow.process_cv_results() == 1
    assert await flow.process_cv_results() == 0
    assert flow.store.get_day_progress("2026-10-04")["lyosha"] is True
    from prompts import REDO_WORDS
    for r in sender.replies:
        assert not any(w in str(r).lower() for w in REDO_WORDS)


class _FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.posted = []

    def claim(self):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def download(self, path, dest):
        open(dest, "wb").write(b"v")

    def post_result(self, rid, payload):
        self.posted.append((rid, payload))
        return 200


def test_box_worker_long_poll_and_backoff():
    sleeps = []
    job = {"request_id": "rw", "claim_token": "t", "video_url": "/cv/jobs/rw/video"}
    client = _FakeClient([None, RuntimeError("net"), RuntimeError("net"), job, None])
    w = BoxCVWorker(client, analyzer=lambda p: {"reps": 3, "exercise": "pushup"}, sleep=sleeps.append)
    for _ in range(5):
        try:
            w.step()
            w.errors = 0
        except Exception:
            sleeps.append(BACKOFF_STEPS[min(w.errors, len(BACKOFF_STEPS) - 1)])
            w.errors += 1
    assert sleeps == [1, 2]          # backoff лише на помилках; 204 -> одразу новий claim
    assert client.posted[0][0] == "rw" and client.posted[0][1]["ok"] is True
    assert client.posted[0][1]["result"]["reps"] == 3


def test_heavy_wrapper(monkeypatch):
    monkeypatch.delenv("CV_HEAVY", raising=False)
    cmd = heavy_cv_command("/v.mp4", "/o.json")
    assert cmd[:5] == ["heavy", "--who", "SportNerdBot-CV", "--max", "600"]
    assert cmd[-3:] == ["cv_worker.cv_job", "/v.mp4", "/o.json"]
