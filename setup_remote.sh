#!/bin/bash
set -e

echo "=== 1. Updating apt and installing python3-venv ==="
apt-get update -y
apt-get install -y python3 python3-pip python3-venv

echo "=== 2. Creating /opt/sportnerdbot and moving files ==="
mkdir -p /opt/sportnerdbot
cp /home/OleksandrPolishchuk/bot.py /opt/sportnerdbot/
cp /home/OleksandrPolishchuk/.env /opt/sportnerdbot/
cp /home/OleksandrPolishchuk/challenge.db /opt/sportnerdbot/

echo "=== 3. Creating venv and installing packages ==="
python3 -m venv /opt/sportnerdbot/venv
/opt/sportnerdbot/venv/bin/pip install --upgrade pip
/opt/sportnerdbot/venv/bin/pip install aiogram apscheduler google-genai aiosqlite pytz python-dotenv

echo "=== 4. Setting up systemd service ==="
cat << 'EOF' > /etc/systemd/system/sportnerdbot.service
[Unit]
Description=SportNerd Telegram Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/sportnerdbot
ExecStart=/opt/sportnerdbot/venv/bin/python /opt/sportnerdbot/bot.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "=== 5. Enabling and starting service ==="
systemctl daemon-reload
systemctl enable --now sportnerdbot.service
systemctl status sportnerdbot.service --no-pager
echo "=== Done! ==="
