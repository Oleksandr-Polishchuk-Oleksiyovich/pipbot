#!/usr/bin/env bash
# Auto-deploy: якщо origin/main має новий коміт — той самий деплой, що й вручну. Логи -> journald.
set -u

REPO="${REPO:-/opt/sportnerdbot/repo}"
CURRENT_REV_FILE="${CURRENT_REV_FILE:-/opt/sportnerdbot/current/REVISION}"
FAILED_FILE="${FAILED_FILE:-/var/lib/sportnerdbot/autodeploy_failed_sha}"
LOCK_FILE="${LOCK_FILE:-/run/sportnerd-autodeploy.lock}"
VM_CONFIG_FILE="${VM_CONFIG_FILE:-$REPO/deploy/vm_config.env}"
ENV_FILE="${ENV_FILE:-/etc/sportnerdbot/env}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
DRY_RUN="${DRY_RUN:-0}"
DB="${DB_PATH:-/var/lib/sportnerdbot/challenge.db}"

ENV_CHANGED=0

sync_vm_config_and_units() {
    local interval="1min"
    if [ -f "$VM_CONFIG_FILE" ]; then
        local raw_val
        raw_val=$(grep -E '^[[:space:]]*AUTODEPLOY_INTERVAL=' "$VM_CONFIG_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '[:space:]')
        if [ -n "$raw_val" ]; then
            if echo "$raw_val" | grep -Eq '^[0-9]+(s|sec|seconds|m|min|minutes|h|hr|hours)$'; then
                interval="$raw_val"
            else
                echo "Invalid AUTODEPLOY_INTERVAL '$raw_val', falling back to 1min"
            fi
        fi
    fi

    local src_svc="$REPO/deploy/autodeploy/sportnerd-autodeploy.service"
    local src_timer="$REPO/deploy/autodeploy/sportnerd-autodeploy.timer"
    local desired_svc
    local desired_timer

    if [ -f "$src_svc" ]; then
        desired_svc=$(sed "s#/opt/sportnerdbot/repo#$REPO#g" "$src_svc")
    else
        desired_svc="[Unit]
Description=SportNerdBot auto-deploy from origin/main
After=network-online.target

[Service]
Type=oneshot
ExecStart=$REPO/deploy/autodeploy/sportnerd-autodeploy.sh
TimeoutStartSec=900
"
    fi

    if [ -f "$src_timer" ]; then
        desired_timer=$(sed -E "s/OnUnitActiveSec=.*/OnUnitActiveSec=$interval/" "$src_timer")
    else
        desired_timer="[Unit]
Description=SportNerdBot auto-deploy check every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=$interval
AccuracySec=10s

[Install]
WantedBy=timers.target
"
    fi

    mkdir -p "$SYSTEMD_DIR" 2>/dev/null || true
    local target_svc="$SYSTEMD_DIR/sportnerd-autodeploy.service"
    local target_timer="$SYSTEMD_DIR/sportnerd-autodeploy.timer"
    local svc_changed=0
    local timer_changed=0

    local tmp_svc="${target_svc}.tmp.$$"
    printf "%s\n" "$desired_svc" > "$tmp_svc"
    if [ ! -f "$target_svc" ] || ! cmp -s "$target_svc" "$tmp_svc"; then
        mv "$tmp_svc" "$target_svc"
        svc_changed=1
    else
        rm -f "$tmp_svc"
    fi

    local tmp_timer="${target_timer}.tmp.$$"
    printf "%s\n" "$desired_timer" > "$tmp_timer"
    if [ ! -f "$target_timer" ] || ! cmp -s "$target_timer" "$tmp_timer"; then
        mv "$tmp_timer" "$target_timer"
        timer_changed=1
    else
        rm -f "$tmp_timer"
    fi

    local target_path="$SYSTEMD_DIR/sportnerd-autodeploy.path"
    local desired_path="[Unit]
Description=SportNerdBot instant auto-deploy trigger path

[Path]
PathModified=/var/lib/sportnerdbot/autodeploy_trigger
Unit=sportnerd-autodeploy.service

[Install]
WantedBy=multi-user.target"
    local path_changed=0
    local tmp_path="${target_path}.tmp.$$"
    printf "%s\n" "$desired_path" > "$tmp_path"
    if [ ! -f "$target_path" ] || ! cmp -s "$target_path" "$tmp_path"; then
        mv "$tmp_path" "$target_path"
        path_changed=1
    else
        rm -f "$tmp_path"
    fi

    if [ "$DRY_RUN" != "1" ] && [ "$(id -u)" = "0" ] && [ -d /etc/sudoers.d ]; then
        local sudoers_file="/etc/sudoers.d/sportnerd-autodeploy"
        local desired_sudoers="sportnerd ALL=(root) NOPASSWD: /bin/systemctl start sportnerd-autodeploy.service, /bin/systemctl start --no-block sportnerd-autodeploy.service, /bin/systemctl restart sportnerd-autodeploy.timer, /usr/bin/systemctl start sportnerd-autodeploy.service, /usr/bin/systemctl start --no-block sportnerd-autodeploy.service, /usr/bin/systemctl restart sportnerd-autodeploy.timer"
        if [ ! -f "$sudoers_file" ] || [ "$(cat "$sudoers_file" 2>/dev/null)" != "$desired_sudoers" ]; then
            printf "%s\n" "$desired_sudoers" > "$sudoers_file"
            chmod 0440 "$sudoers_file"
        fi
    fi

    if [ "$svc_changed" = "1" ] || [ "$timer_changed" = "1" ] || [ "$path_changed" = "1" ]; then
        if [ "$DRY_RUN" != "1" ]; then
            systemctl daemon-reload
            if [ "$timer_changed" = "1" ]; then
                systemctl restart sportnerd-autodeploy.timer
            fi
            if [ "$path_changed" = "1" ]; then
                systemctl enable --now sportnerd-autodeploy.path 2>/dev/null || true
            fi
        fi
    fi

    if [ -f "$VM_CONFIG_FILE" ] && [ -f "$ENV_FILE" ] && [ -w "$ENV_FILE" ]; then
        python3 -c '
import sys, re

src, dst = sys.argv[1], sys.argv[2]
PROTECTED = {
    "AUTODEPLOY_INTERVAL", "BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY",
    "GATEWAY_HMAC_SECRET", "ADMIN_PASSPHRASE", "ROUTINE_SHARED_SECRET",
    "ROUTINE_SENDER_KEY", "ROUTINE_WEBHOOK_URL", "DB_PATH"
}

try:
    with open(src, "r", encoding="utf-8") as f:
        src_lines = f.readlines()
except Exception:
    sys.exit(0)

new_vars = {}
for line in src_lines:
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    if "=" in line:
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip("\"'\''")
        if k and k not in PROTECTED and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
            new_vars[k] = v

try:
    with open(dst, "r", encoding="utf-8") as f:
        dst_lines = f.readlines()
except Exception:
    sys.exit(0)

seen = set()
updated_lines = []
for line in dst_lines:
    stripped = line.strip()
    if stripped and not stripped.startswith("#") and "=" in stripped:
        k, _ = stripped.split("=", 1)
        k = k.strip()
        if k in new_vars:
            updated_lines.append(f"{k}={new_vars[k]}\n")
            seen.add(k)
            continue
    updated_lines.append(line)

for k, v in new_vars.items():
    if k not in seen:
        if updated_lines and not updated_lines[-1].endswith("\n"):
            updated_lines[-1] += "\n"
        updated_lines.append(f"{k}={v}\n")

if updated_lines != dst_lines:
    with open(dst, "w", encoding="utf-8") as f:
        f.writelines(updated_lines)
    sys.exit(10)
sys.exit(0)
' "$VM_CONFIG_FILE" "$ENV_FILE" 2>/dev/null
        local py_rc=$?
        if [ "$py_rc" -eq 10 ]; then
            ENV_CHANGED=1
        fi
    fi
}

if [ "${AUTODEPLOY_REEXEC:-0}" != "1" ]; then
    mkdir -p "$(dirname "$LOCK_FILE")" 2>/dev/null || true
    exec 9>"$LOCK_FILE"
    flock -n 9 || { echo "another autodeploy run in progress — skip"; exit 0; }
fi

cd "$REPO" || exit 1
git fetch -q origin main || { echo "git fetch failed"; exit 1; }
REMOTE=$(git rev-parse origin/main)
DEPLOYED=$(cat "$CURRENT_REV_FILE" 2>/dev/null || echo none)

if [ "$REMOTE" = "$DEPLOYED" ]; then
    sync_vm_config_and_units
    if [ "$ENV_CHANGED" = "1" ] && [ "$DRY_RUN" != "1" ]; then
        systemctl restart sportnerdbot sportnerd-gateway
    fi
    exit 0
fi

if [ "$(cat "$FAILED_FILE" 2>/dev/null)" = "$REMOTE" ]; then exit 0; fi  # не ретраїмо зламаний коміт щохвилини
INFLIGHT=$(sqlite3 "$DB" "select count(*) from cv_jobs where status in ('queued','claimed')" 2>/dev/null || echo 0)
if [ "${INFLIGHT:-0}" != "0" ]; then echo "new commit ${REMOTE:0:7} but $INFLIGHT CV job(s) in flight — deferring"; exit 0; fi

echo "deploying ${REMOTE:0:7} (current ${DEPLOYED:0:7})"
git pull -q --ff-only origin main || { echo "git pull --ff-only failed"; exit 1; }

if [ "${AUTODEPLOY_REEXEC:-0}" != "1" ]; then
    export AUTODEPLOY_REEXEC=1
    exec "$REPO/deploy/autodeploy/sportnerd-autodeploy.sh"
fi

sync_vm_config_and_units

INSTALL_CV=0 timeout 600 bash "$REPO/deploy/deploy.sh" origin/main 2>&1 | grep -viE "token|secret|key="
rc=${PIPESTATUS[0]}

if [ "$DRY_RUN" != "1" ]; then
    chown -R sportnerd: /opt/sportnerdbot /var/lib/sportnerdbot 2>/dev/null || true
fi

if [ "$rc" = "0" ]; then
    if [ "$DRY_RUN" != "1" ]; then
        systemctl restart sportnerdbot sportnerd-gateway
    fi
    echo "autodeploy ${REMOTE:0:7} ok"
else
    echo "$REMOTE" > "$FAILED_FILE"
    echo "autodeploy ${REMOTE:0:7} FAILED rc=$rc (deploy.sh rollback applies; will not retry this SHA)"
fi
