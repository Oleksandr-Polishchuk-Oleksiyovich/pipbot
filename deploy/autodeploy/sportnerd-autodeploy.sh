#!/usr/bin/env bash
# Auto-deploy: якщо origin/main має новий коміт — той самий деплой, що й вручну. Логи -> journald.
set -u
REPO=/opt/sportnerdbot/repo
DB="${DB_PATH:-/var/lib/sportnerdbot/challenge.db}"
exec 9>/run/sportnerd-autodeploy.lock
flock -n 9 || { echo "another autodeploy run in progress — skip"; exit 0; }
cd "$REPO" || exit 1
git fetch -q origin main || { echo "git fetch failed"; exit 1; }
REMOTE=$(git rev-parse origin/main)
DEPLOYED=$(cat /opt/sportnerdbot/current/REVISION 2>/dev/null || echo none)
[ "$REMOTE" = "$DEPLOYED" ] && exit 0
FAILED_FILE=/var/lib/sportnerdbot/autodeploy_failed_sha
if [ "$(cat "$FAILED_FILE" 2>/dev/null)" = "$REMOTE" ]; then exit 0; fi  # не ретраїмо зламаний коміт щохвилини
INFLIGHT=$(sqlite3 "$DB" "select count(*) from cv_jobs where status in ('queued','claimed')" 2>/dev/null || echo 0)
if [ "${INFLIGHT:-0}" != "0" ]; then echo "new commit ${REMOTE:0:7} but $INFLIGHT CV job(s) in flight — deferring"; exit 0; fi
echo "deploying ${REMOTE:0:7} (current ${DEPLOYED:0:7})"
git pull -q --ff-only origin main || { echo "git pull --ff-only failed"; exit 1; }
INSTALL_CV=0 timeout 600 bash deploy/deploy.sh origin/main 2>&1 | grep -viE "token|secret|key="
rc=${PIPESTATUS[0]}
chown -R sportnerd: /opt/sportnerdbot /var/lib/sportnerdbot
if [ "$rc" = "0" ]; then systemctl restart sportnerdbot sportnerd-gateway; echo "autodeploy ${REMOTE:0:7} ok"; else echo "$REMOTE" > "$FAILED_FILE"; echo "autodeploy ${REMOTE:0:7} FAILED rc=$rc (deploy.sh rollback applies; will not retry this SHA)"; fi
