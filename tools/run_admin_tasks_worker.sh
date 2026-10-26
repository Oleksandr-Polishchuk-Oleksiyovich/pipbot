#!/usr/bin/env bash
export PYTHONIOENCODING=utf-8
# Supervisor loop for the box admin_tasks worker (no systemd --user on the box). Not heavy.
#   start:  nohup tools/run_admin_tasks_worker.sh >/dev/null 2>&1 &
#   stop:   kill "$(cat /tmp/sportnerd-admintasks.pid)"
# Env/.env: CV_GATEWAY_URL, GATEWAY_HMAC_SECRET, ADMIN_TASKS_WEBHOOK_URL, ADMIN_TASKS_WEBHOOK_KEY[, ADMIN_TASKS_WEBHOOK_HEADER].
set -u
cd "$(dirname "$0")/.."
PIDFILE="${ADMIN_TASKS_PIDFILE:-/tmp/sportnerd-admintasks.pid}"
LOG="${ADMIN_TASKS_LOG:-/tmp/sportnerd-admintasks.log}"
PY="${ADMIN_TASKS_PYTHON:-.venv/bin/python}"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "already running (pid $(cat "$PIDFILE"))"; exit 0
fi
echo $$ > "$PIDFILE"
if [ ! -x /home/box/.local/bin/agy ] && [ -x /workspace/bin/agy ]; then
    mkdir -p /home/box/.local/bin && cp -f /workspace/bin/agy /home/box/.local/bin/agy
    sudo -n ln -sf /home/box/.local/bin/agy /usr/local/bin/agy 2>/dev/null || true
fi
ensure_venv() {
    if [ ! -x "$PY" ]; then
        echo "$(date -Is) $PY missing; bootstrapping .venv via uv" >>"$LOG.stderr"
        uv venv .venv --python /usr/bin/python3.13 >>"$LOG.stderr" 2>&1 && \
            uv pip install --python .venv/bin/python -r requirements-cv.txt >>"$LOG.stderr" 2>&1
    fi
}
child=""
trap '[ -n "$child" ] && kill "$child" 2>/dev/null; rm -f "$PIDFILE"; exit 0' TERM INT
backoff=5
while true; do
    ensure_venv
    started=$(date +%s)
    "$PY" -m tools.admin_tasks_worker >>"$LOG.stderr" 2>&1 &
    child=$!
    wait "$child"
    rc=$?
    ran=$(( $(date +%s) - started ))
    if [ "$rc" -eq 78 ]; then
        echo "$(date -Is) admin_tasks_worker: CONFIG ERROR (exit 78, see log above); supervisor stops, fix env/.env and restart" >>"$LOG"
        echo "admin_tasks_worker: CONFIG ERROR (CV_GATEWAY_URL/secret missing); see $LOG" >&2
        rm -f "$PIDFILE"; exit 78
    fi
    # швидкий збій (<30 с) -> експоненційний backoff до 300 с; довга робота -> скидання
    if [ "$ran" -lt 30 ]; then backoff=$(( backoff * 2 > 300 ? 300 : backoff * 2 )); else backoff=5; fi
    echo "$(date -Is) admin_tasks_worker exited (rc=$rc after ${ran}s), restarting in ${backoff}s" >>"$LOG"
    sleep "$backoff"
done
