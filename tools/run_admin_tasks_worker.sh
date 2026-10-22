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
child=""
trap '[ -n "$child" ] && kill "$child" 2>/dev/null; rm -f "$PIDFILE"; exit 0' TERM INT
while true; do
    "$PY" -m tools.admin_tasks_worker >>"$LOG.stderr" 2>&1 &
    child=$!
    wait "$child"
    echo "$(date -Is) admin_tasks_worker exited ($?), restarting in 5s" >>"$LOG"
    sleep 5
done
