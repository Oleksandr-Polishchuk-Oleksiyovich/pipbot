"""
sportnerd-gateway: Lightweight aiohttp HTTP service for SportNerdBot.
Handles /deploy triggers from Grok Bot box, /healthz liveness checks,
and /deploy/<request_id> status queries.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

from aiohttp import web

from core.config import (
    DB_PATH,
    DEPLOY_SCRIPT_PATH,
    DEPLOY_STATUS_DIR,
    GATEWAY_HMAC_SECRET,
    GATEWAY_HOST,
    GATEWAY_PORT,
)
from core.signing import verify_payload
from database.store import Store

logger = logging.getLogger("gateway")

STORE_KEY = web.AppKey("store", Store)
SECRET_KEY = web.AppKey("secret", str)
DEPLOY_SCRIPT_KEY = web.AppKey("deploy_script", str)
STATUS_DIR_KEY = web.AppKey("status_dir", str)
ENV_OVERRIDE_KEY = web.AppKey("env_override", Optional[Dict[str, str]])
TASKS_KEY = web.AppKey("background_tasks", Set[Any])
FLOW_CALLBACK_KEY = web.AppKey("flow_callback", Optional[Any])


async def run_deploy_subprocess(
    deploy_script: str,
    sha: str,
    request_id: str,
    status_file: str,
    status_dir: str,
    env_override: Optional[Dict[str, str]] = None,
) -> None:
    """Виконує скрипт deploy.sh в окремому асинхронному процесі та оновлює статус-файл."""
    proc_env = os.environ.copy()
    if env_override:
        proc_env.update(env_override)
    proc_env["STATUS_FILE"] = status_file
    proc_env["STATUS_DIR"] = status_dir

    logger.info("Executing deploy script %s for sha=%s (request_id=%s)", deploy_script, sha, request_id)
    try:
        proc = await asyncio.create_subprocess_exec(
            deploy_script,
            sha,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=proc_env,
        )
        stdout, stderr = await proc.communicate()
        exit_code = proc.returncode
        stdout_str = stdout.decode("utf-8", errors="replace")
        stderr_str = stderr.decode("utf-8", errors="replace")

        logger.info("Deploy script finished with exit code %d (request_id=%s)", exit_code, request_id)

        # Перевіряємо чи deploy.sh уже зберіг фінальний статус (наприклад rolled_back)
        if os.path.exists(status_file):
            try:
                with open(status_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    if saved.get("status") in ("ok", "rolled_back", "failed"):
                        return
            except Exception:
                pass

        final_status = "ok" if exit_code == 0 else "failed"
        detail = stdout_str if exit_code == 0 else f"{stdout_str}\n{stderr_str}".strip()

        status_data = {
            "status": final_status,
            "sha": sha,
            "request_id": request_id,
            "exit_code": exit_code,
            "detail": detail,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(status_data, f)
    except Exception as e:
        logger.exception("Subprocess execution error for deploy request %s: %s", request_id, e)
        status_data = {
            "status": "failed",
            "sha": sha,
            "request_id": request_id,
            "detail": str(e),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            with open(status_file, "w", encoding="utf-8") as f:
                json.dump(status_data, f)
        except Exception:
            pass


async def handle_healthz(request: web.Request) -> web.Response:
    """GET /healthz — health check для моніторингу та systemd."""
    return web.json_response({"status": "ok"})


async def handle_post_deploy(request: web.Request) -> web.Response:
    """
    POST /deploy
    Contract: {contract_version, sha, request_id, chat_id, reply_to_message_id, reply_text, ts, signature}
    - Перевіряє HMAC підпис (GATEWAY_HMAC_SECRET) та вік timestamp (±5 хв).
    - Записує відповідь у pending_replies (T-13 Store).
    - Ідемпотентно: повторний request_id повертає поточний статус (no-op).
    - Запускає deploy.sh в subprocess, повертає 202 Accepted.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    if not isinstance(data, dict):
        return web.json_response({"error": "invalid_payload", "detail": "Expected JSON object"}, status=400)

    # Обов'язкові поля
    for req_field in ("request_id", "sha", "ts", "signature"):
        if req_field not in data:
            return web.json_response(
                {"error": "missing_required_fields", "detail": f"Field '{req_field}' is required"},
                status=400,
            )

    request_id = str(data["request_id"])
    sha = str(data["sha"])
    secret = request.app[SECRET_KEY]

    # Валідація HMAC і timestamp window (±5 хв)
    valid, reason = verify_payload(data, secret=secret, max_age_seconds=300)
    if not valid:
        logger.warning("Deploy auth failed for request_id=%s: %s", request_id, reason)
        return web.json_response({"error": "unauthorized", "detail": reason}, status=401)

    # T-52 Guardrails: перевірка заборонених зон (.env, deploy/*, systemd, etc.)
    files_changed = data.get("files", [])
    branch = data.get("branch", "")
    requires_approval = data.get("requires_approval", False)

    from core.guardrails import check_changes_forbidden
    is_forbidden, forbidden_reasons = check_changes_forbidden(files_changed) if files_changed else (False, [])

    if is_forbidden or requires_approval or branch.startswith("auto/"):
        store: Store = request.app[STORE_KEY]
        audit = store.get_admin_audit(request_id)
        if audit is None or audit.status != "approved":
            logger.warning(
                "Deploy blocked for request_id=%s: forbidden zone change requires approval (%s)",
                request_id,
                forbidden_reasons,
            )
            if audit is None:
                store.create_admin_audit(
                    request_id=request_id,
                    request=f"Deploy for sha {sha}",
                    plan="; ".join(forbidden_reasons),
                    status="waiting_approval",
                )
            else:
                store.update_admin_audit(request_id, status="waiting_approval")
            return web.json_response(
                {
                    "error": "approval_required",
                    "request_id": request_id,
                    "status": "waiting_approval",
                    "detail": "Changes touch forbidden zones (.env, deploy/*, systemd, etc.) and require /approve <request_id>",
                    "reasons": forbidden_reasons,
                },
                status=403,
            )

    status_dir = request.app[STATUS_DIR_KEY]
    os.makedirs(status_dir, exist_ok=True)
    status_file = os.path.join(status_dir, f"{request_id}.json")

    # Idempotency check: якщо такий request_id уже отримано
    if os.path.exists(status_file):
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                existing_status = json.load(f)
            logger.info("Idempotent duplicate deploy request received: %s", request_id)
            return web.json_response(
                {
                    "status": existing_status.get("status", "running"),
                    "request_id": request_id,
                    "sha": existing_status.get("sha", sha),
                    "message": "already_accepted",
                },
                status=202,
            )
        except Exception:
            pass

    # Запис у pending_replies (T-13 Store)
    reply_text = data.get("reply_text")
    chat_id = data.get("chat_id")
    reply_to_message_id = data.get("reply_to_message_id")
    if reply_text and chat_id is not None:
        try:
            chat_id_int = int(chat_id)
            reply_to_int = int(reply_to_message_id) if reply_to_message_id is not None else None
            store: Store = request.app[STORE_KEY]
            store.add_pending_reply(
                request_id=request_id,
                chat_id=chat_id_int,
                text=str(reply_text),
                reply_to_message_id=reply_to_int,
            )
            logger.info("Saved pending_reply for request_id=%s chat_id=%d", request_id, chat_id_int)
        except Exception as e:
            logger.warning("Failed to save pending_reply for %s: %s", request_id, e)

    # Початковий статус 'running'
    initial_status = {
        "status": "running",
        "sha": sha,
        "request_id": request_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(status_file, "w", encoding="utf-8") as f:
        json.dump(initial_status, f)

    # Асинхронний запуск deploy.sh
    deploy_script = request.app[DEPLOY_SCRIPT_KEY]
    task = asyncio.create_task(
        run_deploy_subprocess(
            deploy_script=deploy_script,
            sha=sha,
            request_id=request_id,
            status_file=status_file,
            status_dir=status_dir,
            env_override=request.app.get(ENV_OVERRIDE_KEY),
        )
    )
    request.app[TASKS_KEY].add(task)
    task.add_done_callback(request.app[TASKS_KEY].discard)

    return web.json_response(
        {"status": "running", "request_id": request_id, "sha": sha},
        status=202,
    )


async def handle_get_deploy_status(request: web.Request) -> web.Response:
    """
    GET /deploy/<request_id>
    Повертає поточний статус деплою зі статус-файлу.
    """
    request_id = request.match_info.get("request_id", "")
    status_dir = request.app[STATUS_DIR_KEY]
    status_file = os.path.join(status_dir, f"{request_id}.json")

    if not os.path.isfile(status_file):
        return web.json_response({"error": "not_found", "request_id": request_id}, status=404)

    try:
        with open(status_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return web.json_response(data, status=200)
    except Exception as e:
        logger.error("Error reading status file %s: %s", status_file, e)
        return web.json_response({"error": "read_error", "detail": str(e)}, status=500)


async def handle_post_callback(request: web.Request) -> web.Response:
    """
    POST /callback
    Contract 8.2: box / worker -> gateway /callback
    Handles both 'video' and 'admin' results with HMAC validation,
    idempotency by request_id, updates to offload_requests/video_checks,
    and pending_replies storage for user-facing responses.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    if not isinstance(data, dict):
        return web.json_response({"error": "invalid_payload"}, status=400)

    for req_field in ("request_id", "ts", "signature"):
        if req_field not in data:
            return web.json_response(
                {"error": "missing_required_fields", "detail": f"Field '{req_field}' is required"},
                status=400,
            )

    request_id = str(data["request_id"])
    secret = request.app[SECRET_KEY]

    valid, reason = verify_payload(data, secret=secret, max_age_seconds=300)
    if not valid:
        logger.warning("Callback auth failed for request_id=%s: %s", request_id, reason)
        return web.json_response({"error": "unauthorized", "detail": reason}, status=401)

    store: Store = request.app[STORE_KEY]
    offload_req = store.get_offload_request(request_id)
    video_check = store.get_video_check(request_id)

    # Idempotency check: якщо запит уже завершений (done / failed / expired)
    if offload_req and offload_req.status in ("done", "failed"):
        logger.info("Idempotent duplicate callback for offload request %s (status=%s)", request_id, offload_req.status)
        return web.json_response(
            {"status": "accepted", "request_id": request_id, "message": "already_processed"},
            status=202,
        )
    if video_check and video_check.status in ("done", "failed") and not offload_req:
        logger.info("Idempotent duplicate callback for video check %s (status=%s)", request_id, video_check.status)
        return web.json_response(
            {"status": "accepted", "request_id": request_id, "message": "already_processed"},
            status=202,
        )

    status_val = data.get("status", "done")
    result = data.get("result", {})
    commit_sha = data.get("commit_sha")
    reply_text = data.get("reply_text")

    # Визначення типу callback: video або admin
    req_type = data.get("type")
    if not req_type:
        if offload_req:
            req_type = offload_req.type
        elif (isinstance(result, dict) and "exercise" in result) or video_check:
            req_type = "video"
        else:
            req_type = "admin"

    if req_type == "video":
        # Оновлення таблиці video_checks
        if video_check or (isinstance(result, dict) and "exercise" in result):
            store.update_video_check(
                request_id,
                status=status_val,
                exercise=result.get("exercise") if isinstance(result, dict) else None,
                reps=result.get("reps") if isinstance(result, dict) else None,
                confidence=result.get("confidence") if isinstance(result, dict) else None,
                flags=result.get("flags") if isinstance(result, dict) else None,
                valid=result.get("valid") if isinstance(result, dict) else None,
                analyzer_version=result.get("analyzer_version") if isinstance(result, dict) else None,
                finished=True,
            )

        if offload_req:
            store.update_offload_request(request_id, status=status_val, commit_sha=commit_sha)

        # Якщо надано reply_text — зберігаємо в pending_replies
        if reply_text:
            check = store.get_video_check(request_id)
            chat_id = check.chat_id if check else (offload_req.chat_id if offload_req else data.get("chat_id"))
            reply_to = check.message_id if check else (
                offload_req.reply_to_message_id or offload_req.message_id if offload_req else data.get("reply_to_message_id")
            )
            if chat_id is not None:
                store.add_pending_reply(
                    request_id=f"cb_{request_id}",
                    chat_id=int(chat_id),
                    text=str(reply_text),
                    reply_to_message_id=int(reply_to) if reply_to is not None else None,
                )

    else:
        # req_type == "admin"
        if offload_req:
            store.update_offload_request(request_id, status=status_val, commit_sha=commit_sha)
        else:
            store.create_offload_request(
                request_id=request_id,
                type="admin",
                chat_id=data.get("chat_id"),
                message_id=data.get("message_id"),
                reply_to_message_id=data.get("reply_to_message_id"),
                status=status_val,
                commit_sha=commit_sha,
            )

        # Оновлення admin_audit (T-52)
        audit = store.get_admin_audit(request_id)
        audit_status = "deployed" if status_val == "done" else "failed"
        if audit:
            store.update_admin_audit(request_id, status=audit_status, commit_sha=commit_sha)
        else:
            store.create_admin_audit(
                request_id=request_id,
                request=f"Admin callback ({status_val})",
                commit_sha=commit_sha,
                status=audit_status,
            )

        if reply_text:
            chat_id = data.get("chat_id") or (offload_req.chat_id if offload_req else None)
            reply_to = data.get("reply_to_message_id") or (
                offload_req.reply_to_message_id or offload_req.message_id if offload_req else None
            )
            if chat_id is not None:
                store.add_pending_reply(
                    request_id=f"cb_{request_id}",
                    chat_id=int(chat_id),
                    text=str(reply_text),
                    reply_to_message_id=int(reply_to) if reply_to is not None else None,
                )

    # Якщо підключено flow callback — запускаємо його
    flow_cb = request.app.get(FLOW_CALLBACK_KEY)
    if callable(flow_cb):
        try:
            res = flow_cb(request_id)
            if asyncio.iscoroutine(res):
                asyncio.create_task(res)
        except Exception as e:
            logger.error("Error executing flow_callback for %s: %s", request_id, e)

    return web.json_response({"status": "accepted", "request_id": request_id}, status=202)


async def handle_get_offload_status(request: web.Request) -> web.Response:
    """
    GET /offload/<request_id>
    Повертає поточний стан офлоад-запиту з таблиці offload_requests.
    """
    request_id = request.match_info.get("request_id", "")
    store: Store = request.app[STORE_KEY]
    offload_req = store.get_offload_request(request_id)
    if not offload_req:
        return web.json_response({"error": "not_found", "request_id": request_id}, status=404)

    payload_data = None
    if offload_req.payload:
        try:
            payload_data = json.loads(offload_req.payload)
        except Exception:
            payload_data = offload_req.payload

    return web.json_response(
        {
            "request_id": offload_req.request_id,
            "type": offload_req.type,
            "status": offload_req.status,
            "commit_sha": offload_req.commit_sha,
            "attempts": offload_req.attempts,
            "chat_id": offload_req.chat_id,
            "message_id": offload_req.message_id,
            "reply_to_message_id": offload_req.reply_to_message_id,
            "author_slug": offload_req.author_slug,
            "payload": payload_data,
            "expires_at": offload_req.expires_at,
            "created_at": offload_req.created_at,
            "updated_at": offload_req.updated_at,
        },
        status=200,
    )


def create_app(
    store: Optional[Store] = None,
    secret: Optional[str] = None,
    deploy_script: Optional[str] = None,
    status_dir: Optional[str] = None,
    env_override: Optional[Dict[str, str]] = None,
    flow_callback: Optional[Any] = None,
    cv_options: Optional[Dict[str, Any]] = None,
) -> web.Application:
    """Створює екземпляр aiohttp Application для sportnerd-gateway."""
    app = web.Application()

    app[STORE_KEY] = store if store is not None else Store(DB_PATH)
    app[SECRET_KEY] = secret if secret is not None else GATEWAY_HMAC_SECRET
    app[DEPLOY_SCRIPT_KEY] = deploy_script if deploy_script is not None else DEPLOY_SCRIPT_PATH
    app[STATUS_DIR_KEY] = status_dir if status_dir is not None else DEPLOY_STATUS_DIR
    app[ENV_OVERRIDE_KEY] = env_override
    app[TASKS_KEY] = set()
    app[FLOW_CALLBACK_KEY] = flow_callback

    app.router.add_get("/healthz", handle_healthz)
    app.router.add_post("/deploy", handle_post_deploy)
    app.router.add_get("/deploy/{request_id}", handle_get_deploy_status)
    app.router.add_get("/offload/{request_id}", handle_get_offload_status)
    app.router.add_post("/callback", handle_post_callback)

    from gateway.cv_queue import setup_cv_routes
    setup_cv_routes(app, **(cv_options or {}))

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    app = create_app()
    logger.info("Starting sportnerd-gateway on %s:%d", GATEWAY_HOST, GATEWAY_PORT)
    # keepalive > long-poll (25s) — щоб з'єднання box worker-а не рвалися між claim-ами
    web.run_app(app, host=GATEWAY_HOST, port=GATEWAY_PORT, keepalive_timeout=75)


if __name__ == "__main__":
    main()
