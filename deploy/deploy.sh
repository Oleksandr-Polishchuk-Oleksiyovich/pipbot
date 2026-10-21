#!/usr/bin/env bash
#
# SportNerdBot Release-dir Deploy Script
# Workflow: fetch → checkout (releases/<sha>) → venv check → db backup →
#           migrations (database.migrate) → smoke test → atomic symlink switch →
#           systemctl restart → health check → (rollback if fail) → prune old releases
#
set -euo pipefail

TARGET_REF="${1:-origin/main}"

APP_DIR="${APP_DIR:-/opt/sportnerdbot}"
REPO_DIR="${REPO_DIR:-$APP_DIR/repo}"
RELEASES_DIR="${RELEASES_DIR:-$APP_DIR/releases}"
CURRENT_LINK="${CURRENT_LINK:-$APP_DIR/current}"
SHARED_DIR="${SHARED_DIR:-$APP_DIR/shared}"
VENV_DIR="${VENV_DIR:-$APP_DIR/venv}"
DB_PATH="${DB_PATH:-/var/lib/sportnerdbot/challenge.db}"

if [ -z "${BACKUP_DIR:-}" ]; then
    if [ -d "/var/lib/sportnerdbot" ] && [ -w "/var/lib/sportnerdbot" ]; then
        BACKUP_DIR="/var/lib/sportnerdbot/backups"
    else
        BACKUP_DIR="$APP_DIR/backups"
    fi
fi

if [ -z "${STATUS_DIR:-}" ]; then
    if [ -d "/var/lib/sportnerdbot" ] && [ -w "/var/lib/sportnerdbot" ]; then
        STATUS_DIR="/var/lib/sportnerdbot/deploy_status"
    else
        STATUS_DIR="$APP_DIR/deploy_status"
    fi
fi
STATUS_FILE="${STATUS_FILE:-}"
SERVICE_NAME="${SERVICE_NAME:-sportnerdbot}"
KEEP_RELEASES="${KEEP_RELEASES:-5}"
HEALTH_WAIT_SEC="${HEALTH_WAIT_SEC:-5}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8443/healthz}"

DRY_RUN="${DRY_RUN:-0}"
SKIP_FETCH="${SKIP_FETCH:-0}"
SIMULATE_HEALTH_FAIL="${SIMULATE_HEALTH_FAIL:-0}"
SIMULATE_SMOKE_FAIL="${SIMULATE_SMOKE_FAIL:-0}"
SIMULATE_MIGRATE_FAIL="${SIMULATE_MIGRATE_FAIL:-0}"
SMOKE_TEST_CMD="${SMOKE_TEST_CMD:-}"
PYTHON_BIN="${PYTHON_BIN:-}"

# 0. Знаходимо робочий інтерпретатор python
if [ -z "$PYTHON_BIN" ]; then
    if [ -x "$VENV_DIR/bin/python" ]; then
        PYTHON_BIN="$VENV_DIR/bin/python"
    elif command -v python3 >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python3)"
    else
        PYTHON_BIN="$(command -v python)"
    fi
fi

# Знаходимо директорію git-репозиторію
if [ -d "$REPO_DIR/.git" ]; then
    cd "$REPO_DIR"
elif git rev-parse --show-toplevel >/dev/null 2>&1; then
    cd "$(git rev-parse --show-toplevel)"
fi

# Хелпер запису статусу
write_status() {
    local status="$1"
    local sha="$2"
    local detail="${3:-}"
    local ts
    ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date +"%Y-%m-%dT%H:%M:%SZ")

    local payload="{\"status\":\"$status\",\"sha\":\"$sha\",\"detail\":\"$detail\",\"timestamp\":\"$ts\"}"

    if [ -n "$STATUS_FILE" ]; then
        mkdir -p "$(dirname "$STATUS_FILE")"
        printf '%s\n' "$payload" > "$STATUS_FILE"
    fi

    if [ -n "$STATUS_DIR" ]; then
        mkdir -p "$STATUS_DIR"
        printf '%s\n' "$payload" > "$STATUS_DIR/${sha}.json"
        printf '%s\n' "$payload" > "$STATUS_DIR/last_deploy.json"
    fi
}

echo "=== Starting deployment for ref: $TARGET_REF ==="

# 1. Fetch та визначення SHA
if [ "$SKIP_FETCH" != "1" ] && [ "$DRY_RUN" != "1" ]; then
    echo "Fetching latest changes from remotes..."
    git fetch --all --prune --quiet 2>/dev/null || true
fi

SHA=$(git rev-parse "$TARGET_REF" 2>/dev/null || echo "$TARGET_REF")
if [ -z "$SHA" ]; then
    echo "ERROR: Unable to resolve git ref: $TARGET_REF" >&2
    exit 1
fi
echo "Resolved target SHA: $SHA"

TARGET_RELEASE="$RELEASES_DIR/$SHA"
mkdir -p "$RELEASES_DIR"

if [ ! -d "$TARGET_RELEASE" ]; then
    mkdir -p "$TARGET_RELEASE"
    if git cat-file -e "$SHA^{commit}" 2>/dev/null; then
        echo "Extracting clean tree for $SHA into $TARGET_RELEASE..."
        git archive "$SHA" | tar -x -C "$TARGET_RELEASE"
    else
        echo "Commit $SHA not in git store; copying working directory..."
        cp -a . "$TARGET_RELEASE"
    fi
else
    echo "Release directory already exists: $TARGET_RELEASE"
fi

# 2. Перевірка venv та requirements.txt (+ requirements-cv.txt для CV worker)
# CV рахує box worker (cv_worker/box_worker.py, long-poll /cv/*), тому на VM за замовчуванням
# лише core (INSTALL_CV=0, без mediapipe). INSTALL_CV=1 — ще й requirements-cv.txt (легасі VM CV).
INSTALL_CV="${INSTALL_CV:-0}"
REQ_FILE="$TARGET_RELEASE/requirements.txt"
REQ_CV_FILE="$TARGET_RELEASE/requirements-cv.txt"
INSTALL_REQ="$REQ_FILE"
if [ "$INSTALL_CV" = "1" ] && [ -f "$REQ_CV_FILE" ]; then
    INSTALL_REQ="$REQ_CV_FILE"
fi
if [ -f "$REQ_FILE" ]; then
    REQ_HASH=""
    if command -v sha256sum >/dev/null 2>&1; then
        REQ_HASH=$(cat "$REQ_FILE" "$INSTALL_REQ" | sha256sum | cut -d' ' -f1)
    elif command -v shasum >/dev/null 2>&1; then
        REQ_HASH=$(cat "$REQ_FILE" "$INSTALL_REQ" | shasum -a 256 | cut -d' ' -f1)
    fi

    CACHE_HASH_FILE="$VENV_DIR/.requirements.sha256"
    PREV_HASH=""
    if [ -f "$CACHE_HASH_FILE" ]; then
        PREV_HASH=$(cat "$CACHE_HASH_FILE" 2>/dev/null || true)
    fi

    if [ -n "$REQ_HASH" ] && [ "$REQ_HASH" != "$PREV_HASH" ]; then
        echo "requirements changed (old=$PREV_HASH, new=$REQ_HASH); updating venv from $(basename "$INSTALL_REQ")..."
        if [ "$DRY_RUN" != "1" ]; then
            if [ ! -d "$VENV_DIR" ]; then
                python3 -m venv "$VENV_DIR"
            fi
            # --no-cache-dir: менше диску/RAM на e2-micro (1GB)
            "$VENV_DIR/bin/pip" install --no-cache-dir --upgrade pip
            "$VENV_DIR/bin/pip" install --no-cache-dir -r "$INSTALL_REQ"
        else
            echo "DRY_RUN: skipping pip install"
        fi
        mkdir -p "$(dirname "$CACHE_HASH_FILE")"
        printf '%s\n' "$REQ_HASH" > "$CACHE_HASH_FILE"
    else
        echo "requirements unchanged; reusing existing venv."
    fi
fi

# 3. Бекап бази даних
if [ -f "$DB_PATH" ] && [ -s "$DB_PATH" ]; then
    mkdir -p "$BACKUP_DIR"
    TS=$(date +"%Y%m%d_%H%M%S")
    BACKUP_FILE="$BACKUP_DIR/${TS}.db"
    if command -v sqlite3 >/dev/null 2>&1; then
        echo "Creating sqlite3 pre-migration backup to $BACKUP_FILE..."
        sqlite3 "$DB_PATH" ".backup '$BACKUP_FILE'"
    fi
fi

# 4. Накат міграцій
if [ "$SIMULATE_MIGRATE_FAIL" = "1" ]; then
    echo "ERROR: Simulated migration failure" >&2
    write_status "failed" "$SHA" "Simulated migration failure"
    exit 1
fi

if [ -d "$TARGET_RELEASE/migrations" ]; then
    echo "Applying migrations via database.migrate..."
    if ! (cd "$TARGET_RELEASE" && PYTHONPATH="$TARGET_RELEASE:${PYTHONPATH:-}" "$PYTHON_BIN" -m database.migrate --db "$DB_PATH" --backup-dir "$BACKUP_DIR"); then
        echo "ERROR: database.migrate failed!" >&2
        write_status "failed" "$SHA" "database.migrate failed"
        exit 1
    fi
fi

# 5. Smoke перевірка
if [ "$SIMULATE_SMOKE_FAIL" = "1" ]; then
    echo "ERROR: Simulated smoke check failure" >&2
    write_status "failed" "$SHA" "Simulated smoke check failure"
    exit 1
fi

echo "Running smoke check: build_app..."
if [ -f "$TARGET_RELEASE/bot.py" ]; then
    if ! (cd "$TARGET_RELEASE" && PYTHONPATH="$TARGET_RELEASE:${PYTHONPATH:-}" "$PYTHON_BIN" -c "import bot; bot.build_app(); print('build_app smoke ok')"); then
        echo "ERROR: Bot build_app smoke check failed!" >&2
        write_status "failed" "$SHA" "build_app smoke check failed"
        exit 1
    fi
fi

if [ -n "$SMOKE_TEST_CMD" ]; then
    echo "Running custom smoke test: $SMOKE_TEST_CMD"
    if ! (cd "$TARGET_RELEASE" && eval "$SMOKE_TEST_CMD"); then
        echo "ERROR: Custom smoke test command failed!" >&2
        write_status "failed" "$SHA" "smoke test command failed"
        exit 1
    fi
elif [ "${SKIP_TESTS:-0}" != "1" ] && [ -d "$TARGET_RELEASE/tests" ]; then
    echo "Running offline test suite..."
    if ! (cd "$TARGET_RELEASE" && SPORTNERD_IN_DEPLOY=1 PYTHONPATH="$TARGET_RELEASE:${PYTHONPATH:-}" "$PYTHON_BIN" -m pytest -q --ignore=tests/integration); then
        echo "ERROR: Offline test suite failed!" >&2
        write_status "failed" "$SHA" "offline test suite failed"
        exit 1
    fi
fi

# 6. Атомарне перемикання symlink та рестарт сервісу
PREV=""
if [ -L "$CURRENT_LINK" ]; then
    PREV=$(readlink "$CURRENT_LINK")
fi

echo "Switching symlink: $CURRENT_LINK -> $TARGET_RELEASE"
mkdir -p "$(dirname "$CURRENT_LINK")"
ln -sfn "$TARGET_RELEASE" "$CURRENT_LINK"

echo "Restarting service: $SERVICE_NAME"
if [ "$DRY_RUN" != "1" ]; then
    if command -v systemctl >/dev/null 2>&1; then
        systemctl restart "$SERVICE_NAME"
    fi
else
    echo "DRY_RUN: skipping systemctl restart"
fi

# 7. Health check
health_check() {
    if [ "$SIMULATE_HEALTH_FAIL" = "1" ]; then
        echo "Health check failed (simulated failure)" >&2
        return 1
    fi

    if [ "$DRY_RUN" = "1" ]; then
        echo "DRY_RUN: simulating health check success"
        return 0
    fi

    echo "Waiting ${HEALTH_WAIT_SEC}s for service to stabilize..."
    sleep "$HEALTH_WAIT_SEC"

    if command -v systemctl >/dev/null 2>&1; then
        if ! systemctl is-active --quiet "$SERVICE_NAME"; then
            echo "Service $SERVICE_NAME is not active" >&2
            return 1
        fi
    fi

    if command -v curl >/dev/null 2>&1; then
        if curl -s -f -m 3 "$HEALTH_URL" >/dev/null 2>&1; then
            echo "Health endpoint $HEALTH_URL responded OK"
            return 0
        fi
    fi

    if command -v journalctl >/dev/null 2>&1; then
        if journalctl -u "$SERVICE_NAME" -n 50 --no-pager 2>/dev/null | grep -qiE "(polling started|bot started|running|application started)"; then
            echo "Journal log confirms service started"
            return 0
        fi
    fi

    return 0
}

# 8. Rollback при збої health check
if ! health_check; then
    echo "ERROR: Health check failed! Initiating automatic rollback..." >&2
    if [ -n "$PREV" ] && [ -d "$PREV" ]; then
        echo "Rolling back symlink $CURRENT_LINK -> $PREV"
        ln -sfn "$PREV" "$CURRENT_LINK"
        if [ "$DRY_RUN" != "1" ] && command -v systemctl >/dev/null 2>&1; then
            systemctl restart "$SERVICE_NAME" || true
        fi
        write_status "rolled_back" "$SHA" "Health check failed; rolled back to $PREV"
    else
        echo "No previous release found to roll back to!" >&2
        write_status "failed" "$SHA" "Health check failed; no previous release"
    fi
    exit 1
fi

# 9. Успіх та prune старих релізів
echo "=== Deployment successful for SHA: $SHA ==="
write_status "ok" "$SHA" "Deploy completed successfully"

prune_releases() {
    local keep="${1:-$KEEP_RELEASES}"
    if [ ! -d "$RELEASES_DIR" ]; then
        return 0
    fi

    echo "Pruning old releases in $RELEASES_DIR (keeping newest $keep)..."
    local current_target=""
    local prev_target=""
    if [ -L "$CURRENT_LINK" ]; then
        current_target=$(readlink -f "$CURRENT_LINK" 2>/dev/null || readlink "$CURRENT_LINK")
    fi
    if [ -n "$PREV" ]; then
        prev_target=$(readlink -f "$PREV" 2>/dev/null || echo "$PREV")
    fi

    local count=0
    for dir in $(ls -1dt "$RELEASES_DIR"/* 2>/dev/null); do
        if [ ! -d "$dir" ]; then
            continue
        fi
        local abs_dir
        abs_dir=$(readlink -f "$dir" 2>/dev/null || echo "$dir")
        if [ "$abs_dir" = "$current_target" ] || [ "$abs_dir" = "$prev_target" ]; then
            continue
        fi

        count=$((count + 1))
        if [ "$count" -gt "$keep" ]; then
            echo "Pruning old release directory: $dir"
            rm -rf "$dir"
        fi
    done
}

prune_releases "$KEEP_RELEASES"
exit 0
