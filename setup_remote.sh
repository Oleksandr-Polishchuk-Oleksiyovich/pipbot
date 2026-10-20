#!/usr/bin/env bash
#
# Remote VM Bootstrap Script for SportNerdBot
# Sets up non-root 'sportnerd' user, release directories, systemd services,
# and shared venv. Does NOT copy secrets (.env is created manually by admin).
#
set -euo pipefail

echo "=== 1. Installing system packages ==="
apt-get update -y
apt-get install -y python3 python3-pip python3-venv sqlite3 curl git

echo "=== 2. Creating non-root user 'sportnerd' ==="
if ! id -u sportnerd >/dev/null 2>&1; then
    useradd -r -m -s /bin/bash sportnerd
    echo "Created user 'sportnerd'"
else
    echo "User 'sportnerd' already exists"
fi

echo "=== 3. Creating directory layout ==="
mkdir -p /opt/sportnerdbot/releases
mkdir -p /opt/sportnerdbot/shared
mkdir -p /var/lib/sportnerdbot/backups
mkdir -p /var/lib/sportnerdbot/deploy_status
mkdir -p /etc/sportnerdbot

chown -R sportnerd:sportnerd /opt/sportnerdbot /var/lib/sportnerdbot /etc/sportnerdbot
chmod 750 /opt/sportnerdbot /var/lib/sportnerdbot
chmod 700 /etc/sportnerdbot

echo "=== 4. Setting up shared virtualenv ==="
if [ ! -d /opt/sportnerdbot/venv ]; then
    python3 -m venv /opt/sportnerdbot/venv
    /opt/sportnerdbot/venv/bin/pip install --upgrade pip
    chown -R sportnerd:sportnerd /opt/sportnerdbot/venv
fi

echo "=== 5. Installing deploy script symlink ==="
if [ -f /opt/sportnerdbot/current/deploy/deploy.sh ]; then
    ln -sfn /opt/sportnerdbot/current/deploy/deploy.sh /usr/local/bin/sportnerd-deploy.sh
    chmod +x /usr/local/bin/sportnerd-deploy.sh
fi

echo "=== 6. Installing systemd service units ==="
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_SRC_DIR="$SCRIPT_DIR/deploy/systemd"

if [ -d "$SYSTEMD_SRC_DIR" ]; then
    cp "$SYSTEMD_SRC_DIR"/* /etc/systemd/system/
else
    # Fallback inline generation if deploy/systemd is absent
    cat << 'EOF' > /etc/systemd/system/sportnerdbot.service
[Unit]
Description=SportNerdBot Telegram Polling Service
After=network.target

[Service]
Type=simple
User=sportnerd
Group=sportnerd
WorkingDirectory=/opt/sportnerdbot/current
EnvironmentFile=/etc/sportnerdbot/env
ExecStart=/opt/sportnerdbot/venv/bin/python bot.py
Restart=always
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/sportnerdbot /opt/sportnerdbot
MemoryMax=550M
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
fi

systemctl daemon-reload
echo "Systemd units installed and daemon reloaded."

echo "=== Setup complete! ==="
echo "MANUAL STEP REQUIRED: Create /etc/sportnerdbot/env with mode 0600 (owner sportnerd:sportnerd)."
echo "Ensure DB_PATH=/var/lib/sportnerdbot/challenge.db is defined in /etc/sportnerdbot/env."
