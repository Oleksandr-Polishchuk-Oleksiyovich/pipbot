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
