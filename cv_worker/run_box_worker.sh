#!/usr/bin/env bash
export PYTHONIOENCODING=utf-8
export CV_HEAVY_BIN="${CV_HEAVY_BIN:-/workspace/bin/heavy}"
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
if [ ! -e /usr/local/bin/heavy ] && [ -x /workspace/bin/heavy ]; then
    sudo -n ln -sf /workspace/bin/heavy /usr/local/bin/heavy 2>/dev/null || true
fi
ensure_venv() {
    if [ ! -x "$PY" ]; then
        echo "$(date -Is) $PY missing; bootstrapping .venv via uv" >>"$LOG"
        uv venv .venv --python /usr/bin/python3.13 >>"$LOG" 2>&1 && \
            uv pip install --python .venv/bin/python -r requirements-cv.txt >>"$LOG" 2>&1
    fi
}
child=""
trap '[ -n "$child" ] && kill "$child" 2>/dev/null; rm -f "$PIDFILE"; exit 0' TERM INT
backoff=5
while true; do
    ensure_venv
    started=$(date +%s)
    "$PY" -m cv_worker.box_worker >>"$LOG" 2>&1 &
    child=$!
    wait "$child"
    rc=$?
    ran=$(( $(date +%s) - started ))
    if [ "$rc" -eq 78 ]; then
        echo "$(date -Is) box_worker: CONFIG ERROR (exit 78, see log above); supervisor stops, fix env/.env and restart" >>"$LOG"
        echo "box_worker: CONFIG ERROR (CV_GATEWAY_URL/secret missing); see $LOG" >&2
        rm -f "$PIDFILE"; exit 78
    fi
    # швидкий збій (<30 с) -> експоненційний backoff до 300 с; довга робота -> скидання
    if [ "$ran" -lt 30 ]; then backoff=$(( backoff * 2 > 300 ? 300 : backoff * 2 )); else backoff=5; fi
    echo "$(date -Is) box_worker exited (rc=$rc after ${ran}s), restarting in ${backoff}s" >>"$LOG"
    sleep "$backoff"
done
