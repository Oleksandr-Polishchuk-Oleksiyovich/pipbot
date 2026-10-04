# Деплой SportNerdBot на GCP VM: бекап → заміна коду → міграція промптів → smoke-імпорт → рестарт.
# Разом із кодом копіює локальний .env (BOT_TOKEN, CHAT_ID, GEMINI_API_KEY).
# Якщо smoke-перевірка (імпорт + build_app) падає — автоматично відкочується на бекап.
# Запуск: powershell -ExecutionPolicy Bypass -File deploy.ps1
$ErrorActionPreference = "Stop"
$gcloud = "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
$vm = @("sportnerd-bot-vm", "--project=gen-lang-client-0185250272", "--zone=us-central1-a")
$bundle = Join-Path $env:TEMP "sportnerd_deploy.tgz"

Set-Location $PSScriptRoot
tar -czf $bundle .env bot.py prompts.py migrate_prompts.py enable_alternating.py core/*.py database/*.py agents/*.py app/*.py
if ($LASTEXITCODE -ne 0) { throw "tar failed" }

& $gcloud compute scp $bundle "$($vm[0]):sportnerd_deploy.tgz" $vm[1] $vm[2] --quiet
if ($LASTEXITCODE -ne 0) { throw "scp failed" }

$remote = @'
set -e
TS=$(date +%Y%m%d_%H%M%S)
BACKUP=/opt/sportnerdbot_backup_$TS.tgz
sudo tar -czf $BACKUP --exclude=venv --exclude=bot.log -C /opt sportnerdbot
echo "Backup: $BACKUP"
sudo systemctl stop sportnerdbot
rollback() {
  echo "!!! Deploy failed — restoring $BACKUP"
  sudo tar -xzf $BACKUP -C /opt
  sudo systemctl start sportnerdbot
  exit 1
}
trap rollback ERR
sudo tar -xzf ~/sportnerd_deploy.tgz -C /opt/sportnerdbot
sudo rm -rf /opt/sportnerdbot/services /opt/sportnerdbot/agents/admin_pipeline.py
cd /opt/sportnerdbot
sudo PYTHONIOENCODING=utf-8 venv/bin/python migrate_prompts.py /opt/sportnerdbot/challenge.db
sudo PYTHONIOENCODING=utf-8 venv/bin/python enable_alternating.py /opt/sportnerdbot/challenge.db
sudo chmod 600 /opt/sportnerdbot/.env
sudo venv/bin/python -c "import bot; bot.build_app(); print('smoke build ok')"
trap - ERR
sudo systemctl start sportnerdbot
sleep 8
systemctl is-active sportnerdbot
sudo journalctl -u sportnerdbot -n 25 --no-pager
'@
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remote.Replace("`r`n", "`n")))
& $gcloud compute ssh $vm[0] $vm[1] $vm[2] --command="echo $b64 | base64 -d | bash; rm -f ~/sportnerd_deploy.tgz"
Remove-Item $bundle
