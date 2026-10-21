#!/usr/bin/env bash
# Supervisor loop for the box CV worker (box has no systemd --user / cron).
#   start:  nohup cv_worker/run_box_worker.sh >/dev/null 2>&1 &
#   stop:   kill "$(cat /tmp/sportnerd-boxcv.pid)"   (kills only this supervisor + its child)
# Requires CV_GATEWAY_URL (and secret via CV_HMAC_SECRET_ENV / CV_ENV_FILE). Logs: $CV_LOG (default /tmp/sportnerd-boxcv.log).
set -u
cd "$(dirname "$0")/.."
PIDFILE="${CV_PIDFILE:-/tmp/sportnerd-boxcv.pid}"
LOG="${CV_LOG:-/tmp/sportnerd-boxcv.log}"
PY="${CV_PYTHON:-.venv/bin/python}"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "already running (pid $(cat "$PIDFILE"))"; exit 0
fi
echo $$ > "$PIDFILE"
child=""
trap '[ -n "$child" ] && kill "$child" 2>/dev/null; rm -f "$PIDFILE"; exit 0' TERM INT
while true; do
    "$PY" -m cv_worker.box_worker >>"$LOG" 2>&1 &
    child=$!
    wait "$child"
    echo "$(date -Is) box_worker exited ($?), restarting in 5s" >>"$LOG"
    sleep 5
done
