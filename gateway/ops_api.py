"""
Read-only діагностика VM для box-агента (HMAC+nonce, як /cv/claim): стан cv_jobs / video_checks / daily_attempts /
users / XP і хвіст логів з редакцією секретів. Нічого не змінює.

  GET /admin/diag?since_days=3&date=YYYY-MM-DD&log_lines=120

Редакція: токени ботів, Bearer-значення, key=value для token/secret/key/password, довгі токеноподібні рядки.
"""
import asyncio
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from aiohttp import web

_REDACT = [
    (re.compile(r"\d{6,}:[A-Za-z0-9_-]{25,}"), "<bot-token>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <redacted>"),
    (re.compile(r"(?i)\b([A-Za-z_]*(?:token|secret|key|passphrase|password|hmac|signature)[A-Za-z_]*)\s*[=:]\s*\S+"), r"\1=<redacted>"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"), "<api-key>"),
    (re.compile(r"\b[A-Za-z0-9_-]{40,}\b"), "<long-token>"),
]


def redact(text: str) -> str:
    for rx, rep in _REDACT:
        text = rx.sub(rep, text)
    return text


def _tail_file(path: str, n: int) -> List[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 400_000))
            data = f.read().decode("utf-8", errors="replace")
        return [redact(l) for l in data.splitlines()[-n:]]
    except OSError:
        return []


def _journal(unit: str, n: int) -> Optional[List[str]]:
    if not shutil.which("journalctl"):
        return None
    try:
        out = subprocess.run(["journalctl", "-u", unit, "-n", str(n), "--no-pager", "-o", "short-iso"],
                             capture_output=True, text=True, timeout=8)
    except Exception:
        return None
    if out.returncode != 0 and not out.stdout:
        return None
    return [redact(l) for l in out.stdout.splitlines()[-n:]]


_STATE_ALLOW = ("progression_settled_through", "cv_last_poll_ts", "wake_last_fire_ts", "wake_notice_ts",
                "autodeploy_deployed_sha", "autodeploy_failed_sha", "last_settle_date", "rollover_done_through")


def _rows(con, sql: str, params=()) -> List[Dict[str, Any]]:
    cur = con.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def snapshot(store, since_days: int, date_iso: Optional[str], log_lines: int) -> Dict[str, Any]:
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%d %H:%M:%S")
    with store._connect() as con:
        out: Dict[str, Any] = {"server_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
        out["cv_jobs_by_status"] = {r["status"]: r["n"] for r in _rows(con, "SELECT status, COUNT(*) n FROM cv_jobs GROUP BY status")}
        out["cv_jobs_recent"] = _rows(con, (
            "SELECT substr(request_id,1,8) rid, status, attempts, last_error, created_at, claimed_at, updated_at, "
            "processing_sent_at, still_sent_at, replied_at FROM cv_jobs WHERE created_at >= ? ORDER BY created_at"), (since,))
        out["cv_jobs_unfinished_or_unreplied"] = _rows(con, (
            "SELECT substr(request_id,1,8) rid, status, attempts, last_error, created_at, claimed_at, replied_at "
            "FROM cv_jobs WHERE status IN ('queued','claimed','failed') OR replied_at IS NULL ORDER BY created_at DESC LIMIT 50"))
        out["video_checks"] = _rows(con, (
            "SELECT id, substr(request_id,1,8) rid, chat_id, message_id, user_slug, exercise, reps, confidence, valid, status, "
            "flags, location, duration_s, created_at, finished_at FROM video_checks WHERE created_at >= ? ORDER BY id"), (since,))
        out["video_checks_by_status"] = {r["status"]: r["n"] for r in _rows(con, "SELECT status, COUNT(*) n FROM video_checks GROUP BY status")}
        out["daily_attempts"] = _rows(con, "SELECT date, user_slug, exercise, best_reps, updated_at FROM daily_attempts "
                                           "WHERE date >= date('now', ?) ORDER BY date, user_slug", (f"-{since_days + 2} days",))
        out["user_progress"] = _rows(con, "SELECT date, user_slug, done, updated_at FROM user_progress "
                                          "WHERE date >= date('now', ?) ORDER BY date, user_slug", (f"-{since_days + 2} days",))
        out["users"] = _rows(con, "SELECT slug, telegram_id, telegram_name, username, display_name, status, lives, max_lives, joined_date FROM users")
        out["user_xp"] = _rows(con, "SELECT user_slug, xp, level, updated_at FROM user_xp")
        out["xp_log_recent"] = _rows(con, "SELECT date, user_slug, delta, reason, created_at FROM xp_log ORDER BY id DESC LIMIT 25")
        out["next_norms"] = _rows(con, "SELECT user_slug, exercise, target, effective_from, source_date FROM next_norms")
        out["pending_replies_by_status"] = {r["status"]: r["n"] for r in _rows(con, "SELECT status, COUNT(*) n FROM pending_replies GROUP BY status")}
        out["admin_audit_recent"] = _rows(con, "SELECT * FROM admin_audit ORDER BY rowid DESC LIMIT 10") if _has(con, "admin_audit") else []
        marks = ",".join("?" for _ in _STATE_ALLOW)
        out["bot_state"] = {r["key"]: r["value"] for r in _rows(con, f"SELECT key, value FROM bot_state WHERE key IN ({marks}) "
                                                                      "OR key LIKE 'ops:%' OR key LIKE 'video_credit:%' OR key LIKE 'autodeploy%'", _STATE_ALLOW)}
        if date_iso:
            out["day"] = date_iso
    return out


def _has(con, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


async def handle_diag(request: web.Request) -> web.Response:
    from gateway.app import STORE_KEY
    from gateway.cv_queue import _auth
    from core.config import DB_PATH
    denied = await _auth(request)
    if denied:
        return denied
    q = request.query
    try:
        since_days = max(1, min(14, int(q.get("since_days", "3"))))
        log_lines = max(0, min(400, int(q.get("log_lines", "120"))))
    except ValueError:
        return web.json_response({"error": "bad_params"}, status=400)
    date_iso = q.get("date") or None
    store = request.app[STORE_KEY]
    data = await asyncio.to_thread(snapshot, store, since_days, date_iso, log_lines)

    def _logs():
        log_dir = os.getenv("LOG_DIR") or os.path.dirname(DB_PATH)
        logs = {"bot.log": _tail_file(os.path.join(log_dir, "bot.log"), log_lines)}
        for unit in ("sportnerd-gateway", "sportnerdbot"):
            j = _journal(unit, log_lines)
            logs[f"journal:{unit}"] = j if j is not None else "unavailable"
        return logs
    data["logs"] = await asyncio.to_thread(_logs) if log_lines else {}
    return web.json_response(data, dumps=lambda o: __import__("json").dumps(o, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------------------------
# Одноразові адмін-операції (HMAC+nonce), ідемпотентні за `key`, з записом у bot_state `ops:<key>`.
#   POST /admin/ops {"op": "...", "key": "<unique>", "params": {...}, "dry_run": false}
# Ops: adjust_user, credit_attempt, send_message, admin_task_add, cv_cleanup.
# Повторний виклик з тим самим key повертає збережений результат і НІЧОГО не виконує вдруге.
# ---------------------------------------------------------------------------------------------
import json
import urllib.error
import urllib.request

_OPS_LOCK = asyncio.Lock()


def _op_adjust_user(store, p: Dict[str, Any]) -> Dict[str, Any]:
    from core import challenge
    from core.progression import apply_xp
    slug = str(p["slug"])
    user = store.get_user(slug)
    if user is None:
        raise ValueError(f"unknown user {slug}")
    before = {"lives": user.lives, "max_lives": user.max_lives, "xp": store.get_xp(slug)[0], "level": store.get_xp(slug)[1]}
    lives, max_lives = user.lives, user.max_lives
    if "lives_set" in p:
        lives = int(p["lives_set"])
    if p.get("lives_delta"):
        lives = (lives or 0) + int(p["lives_delta"])
    if max_lives is not None and lives is not None:
        lives = min(lives, max_lives)
    xp_delta = int(p.get("xp_delta", 0))
    ups = 0
    if xp_delta:
        xp, level = store.get_xp(slug)
        xp, level, ups = apply_xp(xp, level, xp_delta)
        store.set_xp(slug, xp, level)
        store.add_xp_log(str(p.get("date") or ""), slug, xp_delta, str(p.get("reason") or "ops adjust_user"))
        if ups and max_lives is not None:   # правило рівня: +1 max життя і життя до максимуму
            max_lives += ups
            lives = max_lives
    if lives is not None and (lives != user.lives or max_lives != user.max_lives):
        challenge.set_user_lives(store, slug, lives, max_lives)
    u2 = store.get_user(slug)
    after = {"lives": u2.lives, "max_lives": u2.max_lives, "xp": store.get_xp(slug)[0], "level": store.get_xp(slug)[1],
             "level_ups": ups}
    return {"slug": slug, "before": before, "after": after}


def _op_credit_attempt(store, p: Dict[str, Any]) -> Dict[str, Any]:
    """Спроба дня (MAX з наявною) + done, якщо досягнуто норми. XP/життя НЕ чіпає (їх рахує settle)."""
    from datetime import date as _d
    from core import challenge
    slug, date_iso, exercise, reps = str(p["slug"]), str(p["date"]), str(p["exercise"]), int(p["reps"])
    day = _d.fromisoformat(date_iso)
    before_best = store.get_best_attempt(date_iso, slug)
    done_before = bool(store.get_day_progress(date_iso).get(slug, False))
    day_ex = challenge.day_exercise(store, day)
    target = challenge.day_targets(store, day).get(slug)
    if exercise != day_ex:
        return {"slug": slug, "date": date_iso, "skipped": f"exercise {exercise} != day exercise {day_ex}"}
    best = store.record_attempt(date_iso, slug, exercise, reps)
    done = done_before
    if target is not None and best >= target and not done_before:
        store.set_done(slug, True, date_iso)
        done = True
    settled = store.get_state("progression_settled_through", "") >= date_iso
    return {"slug": slug, "date": date_iso, "exercise": exercise, "target": target, "best_before": before_best,
            "best_after": best, "done_before": done_before, "done_after": done, "day_already_settled": settled}


def _telegram_send(token: str, chat_id: int, text: str, reply_to: Optional[int]) -> Dict[str, Any]:
    payload = {"chat_id": chat_id, "text": text}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                 data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "error_code": e.code}


def _op_send_message(store, p: Dict[str, Any], key: str) -> Dict[str, Any]:
    from core.config import BOT_TOKEN
    from database.store import format_ts
    from core.config import TIMEZONE
    chat_id, text = int(p["chat_id"]), str(p["text"])
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not configured on VM")
    # «sending» резервуємо ДО відправки: якщо процес впаде посеред — повторно НЕ шлемо (результат невизначений).
    store.set_state(f"ops:{key}", json.dumps({"op": "send_message", "status": "sending"}))
    resp = _telegram_send(BOT_TOKEN, chat_id, text, p.get("reply_to_message_id"))
    if not resp.get("ok"):
        store.set_state(f"ops:{key}", json.dumps({"op": "send_message", "status": "failed", "telegram_error": resp.get("error_code")}))
        raise RuntimeError(f"telegram sendMessage failed: {resp.get('error_code')}")
    mid = resp["result"]["message_id"]
    try:
        store.save_message(chat_id, "Спорт Зануда", "coach", text, format_ts(datetime.now(TIMEZONE)))
    except Exception:
        pass
    return {"telegram_ok": True, "message_id": mid, "chat_id": chat_id}


def _op_admin_task_add(store, p: Dict[str, Any]) -> Dict[str, Any]:
    """Створює admin_task. status='claimed' — щоб воркер box не пересилав у вебхук (задачу веде виконавець вручну)."""
    status = str(p.get("status", "pending"))
    if status not in ("pending", "claimed"):
        raise ValueError("status must be pending|claimed")
    tid = store.create_admin_task(p.get("chat_id"), p.get("requester_telegram_id"), str(p.get("requester_name") or ""), str(p["text"]))
    if status == "claimed":
        with store._connect() as con:
            con.execute("UPDATE admin_tasks SET status='claimed', updated_at=datetime('now') WHERE id=?", (tid,))
    return {"task_id": tid, "status": status}


def _op_cv_cleanup(store, p: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    """
    Прибирання черги: кожен рядок cv_jobs у queued/claimed, старший за max_age_h (за замовчуванням 3 год), стає failed
    (last_error=ops_cleanup) БЕЗ відповіді в чат (replied_at ставиться); video_checks -> failed. Повертає counts до/після.
    """
    max_age_h = float(p.get("max_age_h", 3))
    with store._connect() as con:
        def counts():
            return {
                "cv_jobs": {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM cv_jobs GROUP BY status")},
                "admin_tasks": {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM admin_tasks GROUP BY status")},
                "video_checks_open": {r[0]: r[1] for r in con.execute(
                    "SELECT status, COUNT(*) FROM video_checks WHERE status NOT IN ('done','failed') GROUP BY status")},
            }
        before = counts()
        stale = [r[0] for r in con.execute(
            "SELECT request_id FROM cv_jobs WHERE status IN ('queued','claimed') AND "
            "(julianday('now') - julianday(created_at)) * 24.0 > ?", (max_age_h,))]
        if not dry_run:
            for rid in stale:
                con.execute("UPDATE cv_jobs SET status='failed', last_error='ops_cleanup', replied_at=datetime('now'), "
                            "updated_at=datetime('now') WHERE request_id=?", (rid,))
                con.execute("UPDATE video_checks SET status='failed' WHERE request_id=? AND status NOT IN ('done','failed')", (rid,))
        after = counts()
    return {"stale_cv_jobs": [r[:8] for r in stale], "before": before, "after": after, "applied": not dry_run}


def _op_wake_test(store, p: Dict[str, Any]) -> Dict[str, Any]:
    """Один реальний вибух вебхука пробудження з VM (з ретраями, як у бойовому шляху). Секрети не повертаються."""
    from core import config as cfg
    from core.wake import BoxWaker, POLL_TS_KEY
    waker = BoxWaker(store, enabled=True, stale_s=cfg.WAKE_STALE_S, wait_s=cfg.WAKE_WAIT_S, attempts=cfg.WAKE_ATTEMPTS,
                     lease_s=cfg.CV_LEASE_S, sleep=lambda s: asyncio.sleep(s))
    if not waker.configured():
        return {"fired": False, "reason": "wake webhook not configured"}
    age_before = waker.poll_age_s()
    accepted = asyncio.run(waker.fire(str(p.get("reason") or "e2e_test"), store.count_cv_jobs("queued")))
    return {"fired": True, "accepted_2xx": accepted, "poll_age_before_s": None if age_before is None else round(age_before, 1)}


def _op_set_leave(store, p: Dict[str, Any]) -> Dict[str, Any]:
    from core.leave import set_leave
    slug = str(p["slug"])
    kind = str(p["kind"])
    start_date = p.get("start_date") or None
    end_date = p.get("end_date") or None
    set_by = p.get("set_by") or "ops"
    reason = p.get("reason") or None
    leave = set_leave(store, slug, kind, start=start_date, end=end_date, set_by=set_by, reason=reason)
    return {"slug": slug, "leave": leave}


def _op_end_leave(store, p: Dict[str, Any]) -> Dict[str, Any]:
    from core.leave import end_leave
    slug = str(p["slug"])
    from_day = p.get("from_day") or None
    ended_by = p.get("ended_by") or "ops"
    leave = end_leave(store, slug, from_day=from_day, ended_by=ended_by)
    return {"slug": slug, "leave": leave}


def _run_op(store, op: str, key: str, params: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    if op == "cv_cleanup":
        return _op_cv_cleanup(store, params, dry_run)
    if dry_run:
        return {"dry_run": True, "op": op, "params": {k: (v if k != "text" else str(v)[:200]) for k, v in params.items()}}
    if op == "adjust_user":
        return _op_adjust_user(store, params)
    if op == "credit_attempt":
        return _op_credit_attempt(store, params)
    if op == "send_message":
        return _op_send_message(store, params, key)
    if op == "wake_test":
        return _op_wake_test(store, params)
    if op == "admin_task_add":
        return _op_admin_task_add(store, params)
    if op == "set_leave":
        return _op_set_leave(store, params)
    if op == "end_leave":
        return _op_end_leave(store, params)
    raise ValueError(f"unknown op {op}")


def execute_op(store, op: str, key: str, params: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    state_key = f"ops:{key}"
    prev = store.get_state(state_key, "")
    if prev and not dry_run and op != "cv_cleanup":
        rec = json.loads(prev)
        if rec.get("status") == "sending":
            return {"status": "uncertain", "detail": "a previous send_message attempt did not finish — NOT resending", "record": rec}
        if rec.get("status") == "done":
            return {"status": "already_done", "record": rec}
        # failed -> можна повторити
    try:
        result = _run_op(store, op, key, params, dry_run)
    except Exception as e:
        if not dry_run:
            store.set_state(state_key, json.dumps({"op": op, "status": "failed", "error": str(e)[:200]}))
        return {"status": "error", "error": str(e)[:300]}
    if dry_run:
        return {"status": "dry_run", "result": result}
    rec = {"op": op, "status": "done", "result": result, "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
    store.set_state(state_key, json.dumps(rec, ensure_ascii=False))
    return {"status": "done", "record": rec}


async def handle_ops(request: web.Request) -> web.Response:
    from gateway.app import STORE_KEY
    from gateway.cv_queue import _auth
    denied = await _auth(request)
    if denied:
        return denied
    try:
        data = json.loads((await request.read()).decode("utf-8"))
        op, key = str(data["op"]), str(data["key"])
        params = data.get("params") or {}
        dry_run = bool(data.get("dry_run", False))
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{3,80}", key):
            raise ValueError("bad key")
    except Exception:
        return web.json_response({"error": "invalid_request"}, status=400)
    async with _OPS_LOCK:
        out = await asyncio.to_thread(execute_op, request.app[STORE_KEY], op, key, params, dry_run)
    status = 200 if out.get("status") != "error" else 422
    return web.json_response(out, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))


def setup_ops_routes(app: web.Application) -> None:
    app.router.add_get("/admin/diag", handle_diag)
    app.router.add_post("/admin/ops", handle_ops)
