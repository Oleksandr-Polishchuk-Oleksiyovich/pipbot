#!/usr/bin/env bash
# Для routine «SportNerdBot box wake»: запустити box-воркери, якщо їх нема (supervisor-скрипти ідемпотентні:
# якщо pidfile живий — "already running"), і переконатись, що cv_worker поллить gateway.
#   tools/ensure_workers.sh        -> exit 0, якщо worker_polling; інакше 1
set -u
cd "$(dirname "$0")/.."
# .env (CV_GATEWAY_URL, секрет) має бути в середовищі ДО старту supervisor-ів, інакше worker падає "CV_GATEWAY_URL is not set"
set -a; [ -f .env ] && . ./.env; set +a
nohup cv_worker/run_box_worker.sh >/dev/null 2>&1 &
nohup tools/run_admin_tasks_worker.sh >/dev/null 2>&1 &
sleep 2
exec .venv/bin/python tools/cv_worker_status.py --wait 60
