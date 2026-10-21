"""
Gateway endpoints для admin_tasks (box-розробник забирає запити, на які немає вузького адмін-тула).

  GET  /admin_tasks?status=...       -> {tasks: [...]}
  GET  /admin_tasks/claim            -> long-poll (до ADMIN_TASK_CLAIM_WAIT_S). 200 {task} або 204.
  POST /admin_tasks/{id}/result      -> {status: done_pending_deploy|rejected, fix_commit, result}

Автентифікація — та сама HMAC+nonce, що й /cv/claim (gateway.cv_queue._auth).
"""
import asyncio
import json
import logging
import time

from aiohttp import web

from gateway.cv_queue import _auth

logger = logging.getLogger("gateway.admin_tasks")

ADMIN_TASK_WAIT_KEY = web.AppKey("admin_task_claim_wait_s", float)
POLL_S = 1.0


async def handle_list(request: web.Request) -> web.Response:
    from gateway.app import STORE_KEY
    denied = await _auth(request)
    if denied:
        return denied
    status = request.query.get("status") or None
    tasks = await asyncio.to_thread(request.app[STORE_KEY].list_admin_tasks, status)
    return web.json_response({"tasks": tasks})


async def handle_claim(request: web.Request) -> web.Response:
    from gateway.app import STORE_KEY
    denied = await _auth(request)
    if denied:
        return denied
    store = request.app[STORE_KEY]
    deadline = time.monotonic() + request.app[ADMIN_TASK_WAIT_KEY]
    while True:
        task = await asyncio.to_thread(store.claim_admin_task)
        if task:
            logger.info("admin_task #%s claimed via gateway", task["id"])
            return web.json_response({"task": task})
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return web.Response(status=204)
        await asyncio.sleep(min(POLL_S, remaining))


async def handle_result(request: web.Request) -> web.Response:
    from gateway.app import STORE_KEY
    denied = await _auth(request)
    if denied:
        return denied
    try:
        task_id = int(request.match_info["id"])
        data = json.loads((await request.read()).decode("utf-8") or "{}")
    except Exception:
        return web.json_response({"error": "invalid_request"}, status=400)
    status = data.get("status")
    if status not in ("done_pending_deploy", "rejected"):
        return web.json_response({"error": "invalid_status"}, status=400)
    fix_commit = (data.get("fix_commit") or None)
    if status == "done_pending_deploy" and not fix_commit:
        return web.json_response({"error": "fix_commit_required"}, status=400)
    task = await asyncio.to_thread(request.app[STORE_KEY].set_admin_task_result, task_id, status,
                                   fix_commit, data.get("result"))
    if task is None:
        return web.json_response({"error": "not_found_or_already_resolved"}, status=409)
    logger.info("admin_task #%s result via gateway: %s %s", task_id, status, fix_commit)
    return web.json_response({"task": task})


def setup_admin_task_routes(app: web.Application, *, claim_wait_s=None) -> None:
    from core.config import CV_CLAIM_WAIT_S
    app[ADMIN_TASK_WAIT_KEY] = float(CV_CLAIM_WAIT_S if claim_wait_s is None else claim_wait_s)
    app.router.add_get("/admin_tasks", handle_list)
    app.router.add_get("/admin_tasks/claim", handle_claim)
    app.router.add_post("/admin_tasks/{id}/result", handle_result)
