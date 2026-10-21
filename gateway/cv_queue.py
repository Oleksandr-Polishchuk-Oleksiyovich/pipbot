"""
Box CV offload: gateway endpoints for the box long-polling worker (cv_worker/box_worker.py).

  POST /cv/claim                -> long-poll (до CV_CLAIM_WAIT_S ~25s). 200 {job} або 204 на таймаут.
  GET  /cv/jobs/{rid}/video     -> байти відео (gateway качає з Telegram на claim; box не знає BOT_TOKEN).
  POST /cv/jobs/{rid}/result    -> {claim_token, ok, result{reps,confidence,flags,...}, timings, error}

Усі запити підписані HMAC (core.signing.sign_request): ts ±5 хв + одноразовий nonce (replay protection).
Lease: claimed job без результату довше CV_LEASE_S (~5 хв) повертається в чергу (max 3 спроби).
Після результату бот (окремий процес) відповідає в чат через ChatFlow.process_cv_results().
"""
import asyncio
import json
import logging
import os
import time
from typing import Callable, Optional

from aiohttp import web

from core.signing import NonceCache, verify_request

logger = logging.getLogger("gateway.cv")

CV_EVENT_KEY = web.AppKey("cv_job_event", asyncio.Event)
CV_NONCES_KEY = web.AppKey("cv_nonces", NonceCache)
CV_WAIT_KEY = web.AppKey("cv_claim_wait_s", float)
CV_LEASE_KEY = web.AppKey("cv_lease_s", int)
CV_VIDEO_DIR_KEY = web.AppKey("cv_video_dir", str)
CV_FETCHER_KEY = web.AppKey("cv_video_fetcher", Optional[Callable])

DB_RECHECK_S = 0.5  # бот — окремий процес, тому окрім Event ще перевіряємо БД кожні 0.5 с


async def _auth(request: web.Request) -> Optional[web.Response]:
    from gateway.app import SECRET_KEY
    body = await request.read()
    ok, reason = verify_request(request.method, request.path, body, request.headers,
                                request.app[SECRET_KEY], nonces=request.app[CV_NONCES_KEY])
    if not ok:
        logger.warning("CV endpoint auth failed (%s %s): %s", request.method, request.path, reason)
        return web.json_response({"error": "unauthorized", "detail": reason}, status=401)
    return None


def notify_job_enqueued(app: web.Application) -> None:
    """In-process сигнал для відкритих long-poll (наприклад, коли enqueue відбувається в процесі gateway)."""
    app[CV_EVENT_KEY].set()


def _video_path(app: web.Application, rid: str) -> str:
    return os.path.join(app[CV_VIDEO_DIR_KEY], f"{rid}.mp4")


async def _ensure_video(app: web.Application, store, rid: str) -> None:
    path = _video_path(app, rid)
    if os.path.exists(path):
        return
    check = store.get_video_check(rid)
    if not check or not check.file_id:
        raise RuntimeError("video_check has no file_id")
    os.makedirs(app[CV_VIDEO_DIR_KEY], exist_ok=True)
    fetch = app[CV_FETCHER_KEY]
    if fetch is None:
        from core.config import BOT_TOKEN
        from core.telegram_files import download_telegram_file
        fetch = lambda file_id, dest: download_telegram_file(BOT_TOKEN, file_id, dest)  # noqa: E731
    tmp = path + ".part"
    await asyncio.to_thread(fetch, check.file_id, tmp)
    os.replace(tmp, path)


async def handle_claim(request: web.Request) -> web.Response:
    from gateway.app import STORE_KEY
    denied = await _auth(request)
    if denied:
        return denied
    app = request.app
    store = app[STORE_KEY]
    event = app[CV_EVENT_KEY]
    deadline = time.monotonic() + app[CV_WAIT_KEY]
    while True:
        job = await asyncio.to_thread(store.claim_cv_job, app[CV_LEASE_KEY])
        if job:
            rid = job["request_id"]
            try:
                await _ensure_video(app, store, rid)
            except Exception as e:
                logger.warning("Video fetch failed for %s: %s", rid, e)
                store.complete_cv_job(rid, job["claim_token"], ok=False, error=f"video_fetch: {e}"[:200])
                continue
            check = store.get_video_check(rid)
            return web.json_response({
                "request_id": rid,
                "claim_token": job["claim_token"],
                "attempt": job["attempts"],
                "lease_s": app[CV_LEASE_KEY],
                "video_url": f"/cv/jobs/{rid}/video",
                "duration_s": check.duration_s if check else None,
                "file_unique_id": check.file_unique_id if check else None,
            })
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return web.Response(status=204)
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=min(DB_RECHECK_S, remaining))
        except asyncio.TimeoutError:
            pass


async def handle_video(request: web.Request) -> web.StreamResponse:
    from gateway.app import STORE_KEY
    denied = await _auth(request)
    if denied:
        return denied
    rid = request.match_info["rid"]
    job = request.app[STORE_KEY].get_cv_job(rid)
    path = _video_path(request.app, rid)
    if not job or job["status"] != "claimed" or not os.path.exists(path):
        return web.json_response({"error": "not_found"}, status=404)
    return web.FileResponse(path)


async def handle_result(request: web.Request) -> web.Response:
    from gateway.app import STORE_KEY
    denied = await _auth(request)
    if denied:
        return denied
    rid = request.match_info["rid"]
    try:
        data = json.loads((await request.read()).decode("utf-8"))
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    store = request.app[STORE_KEY]
    ok = bool(data.get("ok"))
    result = data.get("result") or {}
    new_status = store.complete_cv_job(rid, str(data.get("claim_token", "")), ok=ok,
                                       error=data.get("error"), timings=data.get("timings"))
    if new_status is None:
        return web.json_response({"error": "stale_or_unknown_claim"}, status=409)
    if ok:
        store.update_video_check(
            rid, status="done",
            exercise=result.get("exercise", "none"),
            reps=int(result.get("reps", 0) or 0),
            confidence=float(result.get("confidence", 0.0) or 0.0),
            flags=list(result.get("flags") or []),
            valid=bool(result.get("valid", False)),
            analyzer_version=result.get("analyzer_version", "box-cv"),
            finished=True,
        )
    elif new_status == "failed":
        store.update_video_check(rid, status="failed", flags=["worker_error"], finished=True)
    else:
        store.update_video_check(rid, status="offloaded")
        notify_job_enqueued(request.app)
    if new_status in ("done", "failed"):
        try:
            os.unlink(_video_path(request.app, rid))
        except FileNotFoundError:
            pass
    return web.json_response({"status": new_status, "request_id": rid})


def setup_cv_routes(app: web.Application, *, claim_wait_s: Optional[float] = None, lease_s: Optional[int] = None,
                    video_dir: Optional[str] = None, video_fetcher: Optional[Callable] = None) -> None:
    from core.config import CV_CLAIM_WAIT_S, CV_LEASE_S, CV_VIDEO_DIR
    app[CV_EVENT_KEY] = asyncio.Event()
    app[CV_NONCES_KEY] = NonceCache()
    app[CV_WAIT_KEY] = float(CV_CLAIM_WAIT_S if claim_wait_s is None else claim_wait_s)
    app[CV_LEASE_KEY] = int(CV_LEASE_S if lease_s is None else lease_s)
    app[CV_VIDEO_DIR_KEY] = video_dir or CV_VIDEO_DIR
    app[CV_FETCHER_KEY] = video_fetcher
    app.router.add_post("/cv/claim", handle_claim)
    app.router.add_get("/cv/jobs/{rid}/video", handle_video)
    app.router.add_post("/cv/jobs/{rid}/result", handle_result)
