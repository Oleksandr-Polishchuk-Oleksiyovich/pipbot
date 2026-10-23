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
import os
import time
from typing import Any, Optional

from aiohttp import web

from gateway.cv_queue import _auth

logger = logging.getLogger("gateway.admin_tasks")

ADMIN_TASK_WAIT_KEY = web.AppKey("admin_task_claim_wait_s", float)
AUTODEPLOY_TRIGGER_CMD_KEY = web.AppKey("autodeploy_trigger_cmd", Optional[Any])
AUTODEPLOY_REV_FILE_KEY = web.AppKey("autodeploy_rev_file", Optional[str])
AUTODEPLOY_FAILED_FILE_KEY = web.AppKey("autodeploy_failed_file", Optional[str])
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
    wait_s = request.app[ADMIN_TASK_WAIT_KEY]
    if "wait" in request.query:
        try:
            wait_s = max(0.0, min(120.0, float(request.query["wait"])))
        except (ValueError, TypeError):
            pass
    deadline = time.monotonic() + wait_s
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


async def handle_autodeploy_status(request: web.Request) -> web.Response:
    denied = await _auth(request)
    if denied:
        return denied
    rev_file = request.app.get(AUTODEPLOY_REV_FILE_KEY) or os.getenv("CURRENT_REV_FILE", "/opt/sportnerdbot/current/REVISION")
    failed_file = request.app.get(AUTODEPLOY_FAILED_FILE_KEY) or os.getenv("FAILED_FILE", "/var/lib/sportnerdbot/autodeploy_failed_sha")

    deployed_sha = None
    if os.path.isfile(rev_file):
        try:
            with open(rev_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                deployed_sha = content or None
        except Exception as e:
            logger.warning("Failed to read rev_file %s: %s", rev_file, e)

    failed_sha = None
    if os.path.isfile(failed_file):
        try:
            with open(failed_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                failed_sha = content or None
        except Exception as e:
            logger.warning("Failed to read failed_file %s: %s", failed_file, e)

    return web.json_response({"deployed_sha": deployed_sha, "failed_sha": failed_sha})


async def handle_autodeploy_trigger(request: web.Request) -> web.Response:
    denied = await _auth(request)
    if denied:
        return denied

    rev_file = request.app.get(AUTODEPLOY_REV_FILE_KEY) or os.getenv("CURRENT_REV_FILE", "/opt/sportnerdbot/current/REVISION")
    deployed_sha = None
    if os.path.isfile(rev_file):
        try:
            with open(rev_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                deployed_sha = content or None
        except Exception:
            pass

    trigger_cmd = request.app.get(AUTODEPLOY_TRIGGER_CMD_KEY)
    if trigger_cmd is None and os.getenv("AUTODEPLOY_TRIGGER_CMD"):
        trigger_cmd = os.getenv("AUTODEPLOY_TRIGGER_CMD")

    if trigger_cmd is not None:
        if callable(trigger_cmd):
            res = trigger_cmd()
            if asyncio.iscoroutine(res):
                await res
        elif isinstance(trigger_cmd, (list, tuple)):
            proc = await asyncio.create_subprocess_exec(*trigger_cmd)
            await proc.wait()
        elif isinstance(trigger_cmd, str):
            proc = await asyncio.create_subprocess_shell(trigger_cmd)
            await proc.wait()
    else:
        trig_file = os.getenv("AUTODEPLOY_TRIGGER_FILE", "/var/lib/sportnerdbot/autodeploy_trigger")
        try:
            os.makedirs(os.path.dirname(trig_file), exist_ok=True)
            with open(trig_file, "w", encoding="utf-8") as f:
                f.write(f"{time.time()}\n")
        except Exception as e:
            logger.warning("Failed to touch autodeploy trigger file %s: %s", trig_file, e)
        cmd = ["sudo", "-n", "/bin/systemctl", "start", "--no-block", "sportnerd-autodeploy.service"]
        try:
            proc = await asyncio.create_subprocess_exec(*cmd)
            await proc.wait()
        except Exception as e:
            logger.warning("Failed to run autodeploy trigger command: %s", e)

    return web.json_response({"status": "triggered", "deployed_sha": deployed_sha}, status=202)


def setup_admin_task_routes(
    app: web.Application,
    *,
    claim_wait_s=None,
    trigger_cmd=None,
    rev_file=None,
    failed_file=None,
) -> None:
    from core.config import CV_CLAIM_WAIT_S
    app[ADMIN_TASK_WAIT_KEY] = float(CV_CLAIM_WAIT_S if claim_wait_s is None else claim_wait_s)
    if trigger_cmd is not None:
        app[AUTODEPLOY_TRIGGER_CMD_KEY] = trigger_cmd
    if rev_file is not None:
        app[AUTODEPLOY_REV_FILE_KEY] = rev_file
    if failed_file is not None:
        app[AUTODEPLOY_FAILED_FILE_KEY] = failed_file
    app.router.add_get("/admin_tasks", handle_list)
    app.router.add_get("/admin_tasks/claim", handle_claim)
    app.router.add_post("/admin_tasks/{id}/result", handle_result)
    app.router.add_get("/autodeploy/status", handle_autodeploy_status)
    app.router.add_post("/autodeploy/trigger", handle_autodeploy_trigger)
