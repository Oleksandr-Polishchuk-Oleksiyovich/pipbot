# SportNerdBot Deployment Guide

This directory contains deployment scripts and documentation for SportNerdBot pull-based deployment with atomic symlink releases and automated rollback.

## Architecture Overview

- **Code Releases:** `/opt/sportnerdbot/releases/<sha>`
- **Active Release Symlink:** `/opt/sportnerdbot/current -> /opt/sportnerdbot/releases/<sha>`
- **Shared Virtualenv:** `/opt/sportnerdbot/venv` (cached; re-installed only when `requirements.txt` SHA256 changes)
- **Database (WAL mode):** `/var/lib/sportnerdbot/challenge.db` (separated from code releases)
- **Database Backups:** `/var/lib/sportnerdbot/backups/<timestamp>.db`
- **Deploy Status:** `/var/lib/sportnerdbot/deploy_status/<request_id>.json`

## Release Script (`deploy/deploy.sh`)

`deploy/deploy.sh` performs atomic zero-downtime (or minimal restart window) deployments:

1. **Fetch & Resolve SHA:** Pulls latest references from remotes (`git fetch --all`), resolves target reference (default: `origin/main`).
2. **Release Checkout:** Extracts committed tree into `/opt/sportnerdbot/releases/<sha>` via `git archive`.
3. **Virtualenv Validation:** Compares `requirements.txt` SHA256 against cached hash; runs `pip install` only on dependency changes.
4. **Pre-migration Backup:** Creates an online backup using `sqlite3 .backup` before any schema changes.
5. **Database Migration:** Executes `python -m database.migrate --db $DB_PATH` to bring SQLite schema to HEAD.
6. **Smoke Check:** Runs `python -c "import bot; bot.build_app()"` and fast offline pytest test suite.
7. **Atomic Symlink Switch:** Reads previous symlink target and updates `/opt/sportnerdbot/current` to point to `/opt/sportnerdbot/releases/<sha>`.
8. **Service Restart:** Restarts `sportnerdbot` systemd service (`systemctl restart sportnerdbot`).
9. **Health Verification:** Verifies service status via `systemctl is-active`, journal polling confirmation, or `GET /healthz`.
10. **Automated Rollback:** If health verification fails, atomically restores previous symlink (`current -> $PREV`), restarts service, records status `rolled_back`, and exits with non-zero code.
11. **Release Pruning:** Keeps the newest 5 releases, protecting currently active and previous releases from pruning.

### Usage

```bash
# Deploy latest origin/main
./deploy/deploy.sh

# Deploy specific commit SHA
./deploy/deploy.sh 8a3f2b4c

# Dry-run mode (local testing without real systemd or network calls)
DRY_RUN=1 ./deploy/deploy.sh
```

### Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `APP_DIR` | `/opt/sportnerdbot` | Root application directory |
| `RELEASES_DIR` | `$APP_DIR/releases` | Directory containing versioned releases |
| `CURRENT_LINK` | `$APP_DIR/current` | Atomic symlink used by systemd units |
| `VENV_DIR` | `$APP_DIR/venv` | Shared virtual environment |
| `DB_PATH` | `/var/lib/sportnerdbot/challenge.db` | SQLite database file location |
| `BACKUP_DIR` | `/var/lib/sportnerdbot/backups` | Database backups destination |
| `STATUS_DIR` | `/var/lib/sportnerdbot/deploy_status` | Status files directory (`<sha>.json`) |
| `STATUS_FILE` | `""` | Optional specific status file path |
| `SERVICE_NAME` | `sportnerdbot` | Systemd service to restart |
| `KEEP_RELEASES` | `5` | Number of previous releases to retain |
| `HEALTH_WAIT_SEC` | `5` | Seconds to wait before checking service health |
| `DRY_RUN` | `0` | If `1`, simulates execution without systemd or external commands |

## Automated Rollback Behavior

If any step after symlink switching fails (service fails to start or health check fails):
1. The symlink is restored to the previous target `$PREV`.
2. The service is restarted with the previous release.
3. Status file is updated to `{"status": "rolled_back", ...}`.
4. Process exits with code 1.

## Box CV offload (long-poll)

CV-підрахунок відео винесено з e2-micro VM на Grok Bot box (у box немає вхідних з'єднань, тому box сам тягне задачі).

- **VM (bot):** відео -> `video_checks(status='offloaded')` + `cv_jobs(status='queued')`, одна відповідь `processing` (з `bot_texts`). Scheduler кожні 10 с (`ChatFlow.process_cv_results`) відповідає на `done/failed` (judge + Gemini-тон як раніше) і один раз шле `still_counting`, якщо результату немає довше `CV_STILL_COUNTING_MIN` (5 хв). Ніколи не просить переробити підхід.
- **VM (gateway, `gateway/cv_queue.py`):** `POST /cv/claim` — long-poll до `CV_CLAIM_WAIT_S` (25 с), 200 з job або 204; `GET /cv/jobs/{id}/video` — відео (gateway качає його з Telegram на claim у `CV_VIDEO_DIR`, видаляє після done/failed); `POST /cv/jobs/{id}/result` — `{claim_token, ok, result, timings, error}`. Lease `CV_LEASE_S` (300 с): прострочений claim -> requeue, після 3 спроб -> failed.
- **Auth:** кожен запит підписаний HMAC (`core.signing.sign_request`): `METHOD\nPATH\nTS\nNONCE\nsha256(body)`, заголовки `X-SN-Ts/X-SN-Nonce/X-SN-Signature`, вікно ±5 хв + одноразовий nonce (replay -> 401). Секрет — існуючий `GATEWAY_HMAC_SECRET`.
- **Box worker (`cv_worker/box_worker.py`):** env `CV_GATEWAY_URL`, `CV_HMAC_SECRET_ENV` (назва змінної, default `GATEWAY_HMAC_SECRET`), `CV_ENV_FILE`. Після 204 одразу новий claim; backoff лише на помилках 1/2/5/10/20/30 с. Кожен job: `heavy --who SportNerdBot-CV --max 600 python -m cv_worker.cv_job <video> <out.json>` (15 fps, мін. 12; 640px). Запуск: `nohup cv_worker/run_box_worker.sh >/dev/null 2>&1 &` (supervisor-цикл, pidfile `/tmp/sportnerd-boxcv.pid`; у box немає systemd --user/cron — після рестарту box запустити знову). Залежності на box: `pip install -r requirements-cv.txt` + apt `libegl1 libgles2`.
- **VM deps:** `deploy.sh` за замовчуванням `INSTALL_CV=0` (без mediapipe). `sportnerd-cv.service` на VM вимкнути: `systemctl disable --now sportnerd-cv`.
- **Мережа:** gateway має бути доступний з box: публічний HTTPS (reverse proxy з TLS -> 127.0.0.1:8443, `proxy_read_timeout >= 60s`, `proxy_buffering off` для /cv/claim) і firewall-правило лише на цей порт. aiohttp `keepalive_timeout=75`.
