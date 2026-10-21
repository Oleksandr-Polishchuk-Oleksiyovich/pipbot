#!/usr/bin/env bash
#
# SportNerdBot one-shot deploy from GCP Cloud Shell.
#   (a) firewall tcp:443 -> VM (network tag), resolve external IP
#   (b) over `gcloud compute ssh`: DB backup, swap, git pull, deploy.sh (INSTALL_CV=0),
#       GATEWAY_HMAC_SECRET in /etc/sportnerdbot/env, Caddy (<IP>.sslip.io -> 127.0.0.1:8443),
#       disable sportnerd-cv, restart bot + gateway, health check
#   (c) print IP, gateway URL and secret (only in YOUR terminal)
# Idempotent: safe to re-run.
#
# Usage (Cloud Shell):
#   curl -fsSL https://raw.githubusercontent.com/Oleksandr-Polishchuk-Oleksiyovich/pipbot/main/deploy/cloudshell_deploy.sh | bash
# Overrides: PROJECT, VM_NAME, ZONE, FW_RULE, NET_TAG, REPO_URL, BRANCH
set -euo pipefail

VM_NAME="${VM_NAME:-sportnerd-bot-vm}"
ZONE="${ZONE:-us-central1-a}"
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
PROJECT="${PROJECT:-gen-lang-client-0185250272}"
FW_RULE="${FW_RULE:-sportnerd-allow-https}"
NET_TAG="${NET_TAG:-sportnerd-https}"
REPO_URL="${REPO_URL:-https://github.com/Oleksandr-Polishchuk-Oleksiyovich/pipbot.git}"
BRANCH="${BRANCH:-main}"

G=(--project "$PROJECT")
echo "=== [local] project=$PROJECT vm=$VM_NAME zone=$ZONE ==="

# --- (a) firewall + tag + IP ---------------------------------------------------
if gcloud compute firewall-rules describe "$FW_RULE" "${G[@]}" >/dev/null 2>&1; then
    echo "Firewall rule $FW_RULE already exists"
else
    gcloud compute firewall-rules create "$FW_RULE" "${G[@]}" \
        --network default --direction INGRESS --action ALLOW \
        --rules tcp:443 --source-ranges 0.0.0.0/0 --target-tags "$NET_TAG" \
        --description "SportNerdBot gateway HTTPS (Caddy -> 127.0.0.1:8443)"
fi

TAGS="$(gcloud compute instances describe "$VM_NAME" --zone "$ZONE" "${G[@]}" --format='value(tags.items)')"
if [[ ";${TAGS//,/;};" == *";$NET_TAG;"* ]]; then
    echo "VM already has tag $NET_TAG"
else
    gcloud compute instances add-tags "$VM_NAME" --zone "$ZONE" "${G[@]}" --tags "$NET_TAG"
fi

IP="$(gcloud compute instances describe "$VM_NAME" --zone "$ZONE" "${G[@]}" \
      --format='value(networkInterfaces[0].accessConfigs[0].natIP)')"
if [[ -z "$IP" ]]; then
    echo "ERROR: VM has no external IP" >&2
    exit 1
fi
HOST="${IP}.sslip.io"
echo "External IP: $IP  ->  https://$HOST"

# --- (b) remote part (runs as root on the VM) --------------------------------------
REMOTE_LOG="$(mktemp)"
trap 'rm -f "$REMOTE_LOG"' EXIT

gcloud compute ssh "$VM_NAME" --zone "$ZONE" "${G[@]}" --quiet \
    --command "sudo bash -s -- '$HOST' '$REPO_URL' '$BRANCH'" <<'REMOTE' | tee "$REMOTE_LOG"
set -euo pipefail
HOST="$1"; REPO_URL="$2"; BRANCH="$3"
APP_DIR=/opt/sportnerdbot
REPO_DIR=$APP_DIR/repo
DATA_DIR=/var/lib/sportnerdbot
DB_PATH=$DATA_DIR/challenge.db
ENV_DIR=/etc/sportnerdbot
ENV_FILE=$ENV_DIR/env
TS="$(date +%Y%m%d_%H%M%S)"
export DEBIAN_FRONTEND=noninteractive

echo "=== [vm] packages ==="
need=()
for p in git sqlite3 curl python3-venv openssl; do
    dpkg -s "$p" >/dev/null 2>&1 || need+=("$p")
done
if ((${#need[@]})); then apt-get update -y && apt-get install -y "${need[@]}"; fi

echo "=== [vm] user + layout (as setup_remote.sh) ==="
id -u sportnerd >/dev/null 2>&1 || useradd -r -m -s /bin/bash sportnerd
mkdir -p $APP_DIR/releases $APP_DIR/shared $DATA_DIR/backups $DATA_DIR/deploy_status $ENV_DIR

# Legacy layout detection: where does the currently installed bot run from?
LEGACY_DIR=""
WD="$(systemctl show -p WorkingDirectory --value sportnerdbot 2>/dev/null || true)"
if [[ -n "$WD" && "$WD" != "$APP_DIR/current" && -d "$WD" ]]; then
    LEGACY_DIR="$WD"
    echo "Legacy WorkingDirectory detected: $LEGACY_DIR"
fi

echo "=== [vm] DB backup ==="
backup_db() {  # $1 = db path
    local src="$1" dst
    dst="$DATA_DIR/backups/pre_cloudshell_${TS}_$(basename "$src")"
    sqlite3 "$src" ".backup '$dst'"
    echo "Backup: $src -> $dst"
}
[[ -s "$DB_PATH" ]] && backup_db "$DB_PATH"
if [[ -n "$LEGACY_DIR" && -s "$LEGACY_DIR/challenge.db" ]]; then
    backup_db "$LEGACY_DIR/challenge.db"
fi
[[ -s "$DB_PATH" || -n "$LEGACY_DIR" ]] || echo "No existing DB found (fresh install?)"

echo "=== [vm] swap 2G ==="
if swapon --show=NAME --noheadings | grep -q .; then
    echo "Swap already active: $(swapon --show=NAME,SIZE --noheadings | tr '\n' ' ')"
else
    [[ -f /swapfile ]] || { fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048; }
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null 2>&1 || true
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    echo "Swap enabled"
fi

echo "=== [vm] env file $ENV_FILE ==="
if [[ ! -s "$ENV_FILE" && -n "$LEGACY_DIR" && -s "$LEGACY_DIR/.env" ]]; then
    cp "$LEGACY_DIR/.env" "$ENV_FILE"
    echo "Seeded $ENV_FILE from legacy .env (contents not shown)"
fi
touch "$ENV_FILE"
if grep -qE '^GATEWAY_HMAC_SECRET=.+' "$ENV_FILE"; then
    echo "GATEWAY_HMAC_SECRET already present (kept)"
else
    sed -i '/^GATEWAY_HMAC_SECRET=/d' "$ENV_FILE"
    printf 'GATEWAY_HMAC_SECRET=%s\n' "$(openssl rand -hex 32)" >> "$ENV_FILE"
    echo "GATEWAY_HMAC_SECRET generated"
fi
grep -qE '^DB_PATH=' "$ENV_FILE" || echo "NOTE: DB_PATH not set in env; app default applies"
chown -R sportnerd:sportnerd $ENV_DIR
chmod 700 $ENV_DIR
chmod 600 "$ENV_FILE"

echo "=== [vm] DB move to $DB_PATH (only if missing) ==="
if [[ ! -s "$DB_PATH" && -n "$LEGACY_DIR" && -s "$LEGACY_DIR/challenge.db" ]]; then
    systemctl stop sportnerdbot sportnerd-gateway 2>/dev/null || true
    sqlite3 "$LEGACY_DIR/challenge.db" ".backup '$DB_PATH'"
    echo "Copied legacy DB -> $DB_PATH (original left in place)"
fi

echo "=== [vm] git pull ==="
git config --system --get-all safe.directory 2>/dev/null | grep -qx "$REPO_DIR" \
    || git config --system --add safe.directory "$REPO_DIR"
if [[ -d "$REPO_DIR/.git" ]]; then
    git -C "$REPO_DIR" fetch --prune origin
    git -C "$REPO_DIR" checkout -q "$BRANCH"
    git -C "$REPO_DIR" pull --ff-only origin "$BRANCH"
else
    git clone --branch "$BRANCH" "$REPO_URL" "$REPO_DIR"
fi
echo "Repo HEAD: $(git -C "$REPO_DIR" rev-parse --short HEAD)"

echo "=== [vm] systemd units ==="
for u in sportnerdbot.service sportnerd-gateway.service sportnerdbot-deploy.service sportnerdbot-deploy.timer; do
    install -m 644 "$REPO_DIR/deploy/systemd/$u" /etc/systemd/system/$u
done
systemctl daemon-reload
systemctl enable sportnerdbot sportnerd-gateway >/dev/null
if systemctl list-unit-files sportnerd-cv.service --no-legend 2>/dev/null | grep -q sportnerd-cv; then
    systemctl disable --now sportnerd-cv 2>/dev/null || true
    echo "sportnerd-cv disabled + stopped"
fi

echo "=== [vm] deploy.sh (INSTALL_CV=0) ==="
chown -R sportnerd:sportnerd $APP_DIR $DATA_DIR
export REPO_DIR DB_PATH
if ! INSTALL_CV=0 bash "$REPO_DIR/deploy/deploy.sh" "origin/$BRANCH"; then
    chown -R sportnerd:sportnerd $APP_DIR $DATA_DIR
    echo "ERROR: deploy.sh failed (see output above)" >&2
    exit 1
fi
chown -R sportnerd:sportnerd $APP_DIR $DATA_DIR
ln -sfn $APP_DIR/current/deploy/deploy.sh /usr/local/bin/sportnerd-deploy.sh

echo "=== [vm] Caddy ==="
if ! command -v caddy >/dev/null 2>&1; then
    apt-get install -y debian-keyring debian-archive-keyring apt-transport-https gnupg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -y && apt-get install -y caddy
fi
CADDY_NEW="$(mktemp)"
cat > "$CADDY_NEW" <<CADDY
{
	servers {
		timeouts {
			read_body 120s
			idle 120s
		}
	}
}

$HOST {
	reverse_proxy 127.0.0.1:8443 {
		flush_interval -1
		transport http {
			dial_timeout 10s
			response_header_timeout 90s
			read_timeout 90s
			write_timeout 90s
		}
	}
}
CADDY
if ! cmp -s "$CADDY_NEW" /etc/caddy/Caddyfile; then
    caddy validate --adapter caddyfile --config "$CADDY_NEW"
    install -m 644 "$CADDY_NEW" /etc/caddy/Caddyfile
    echo "Caddyfile updated"
fi
rm -f "$CADDY_NEW"
systemctl enable caddy >/dev/null
systemctl reload caddy 2>/dev/null || systemctl restart caddy

echo "=== [vm] restart bot + gateway ==="
systemctl restart sportnerdbot sportnerd-gateway
sleep 5
rc=0
for s in sportnerdbot sportnerd-gateway caddy; do
    if systemctl is-active --quiet "$s"; then echo "OK  $s active"; else echo "FAIL $s not active"; rc=1; fi
done
if curl -fsS -m 5 http://127.0.0.1:8443/healthz >/dev/null; then
    echo "OK  local gateway /healthz"
else
    echo "FAIL local gateway /healthz"; rc=1
fi
if ((rc)); then
    journalctl -u sportnerdbot -u sportnerd-gateway -n 40 --no-pager || true
    exit 1
fi

SECRET="$(grep -E '^GATEWAY_HMAC_SECRET=' "$ENV_FILE" | tail -n1 | cut -d= -f2- | tr -d '"'"'"' ')"
echo "@@SN_SECRET@@=$SECRET"
REMOTE

SECRET="$(grep -a '^@@SN_SECRET@@=' "$REMOTE_LOG" | tail -n1 | cut -d= -f2- | tr -d '\r')"

# --- public health check (TLS cert issuance can take ~30s on first run) --------
echo "=== [local] public health check https://$HOST/healthz ==="
PUB_OK=0
for _ in $(seq 1 12); do
    if curl -fsS -m 10 "https://$HOST/healthz" >/dev/null 2>&1; then PUB_OK=1; break; fi
    sleep 5
done
if ((PUB_OK)); then echo "OK  public HTTPS /healthz"; else echo "WARN public HTTPS /healthz not reachable yet (check firewall / caddy logs: journalctl -u caddy)"; fi

# --- (c) summary ---------------------------------------------------------------------
cat <<SUMMARY

==================================================================
 VM external IP     : $IP
 CV_GATEWAY_URL     : https://$HOST
 GATEWAY_HMAC_SECRET: $SECRET
==================================================================
 Copy the URL and the secret into the masked form. Do not share them.
SUMMARY
