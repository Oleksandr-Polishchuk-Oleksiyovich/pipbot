"""
Unit tests for deploy/deploy.sh (T-20).
Tests release-dir deployment, atomic symlink switching, rollback on health failure,
smoke check failure protection, and old release pruning in an offline temporary environment.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


DEPLOY_SCRIPT = str(Path(__file__).resolve().parent.parent / "deploy" / "deploy.sh")


def test_deploy_script_syntax():
    """Перевірка синтаксису bash -n deploy/deploy.sh."""
    assert os.path.isfile(DEPLOY_SCRIPT)
    res = subprocess.run(["bash", "-n", DEPLOY_SCRIPT], capture_output=True, text=True)
    assert res.returncode == 0, f"bash -n failed: {res.stderr}"


def init_mock_git_repo(repo_dir: Path) -> str:
    """Ініціалізує простий тестовий git-репозиторій і повертає SHA першого коміту."""
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo_dir, check=True)

    # Мінімальний bot.py для smoke-тесту
    (repo_dir / "bot.py").write_text("def build_app(): return 'ok'\n")
    (repo_dir / "requirements.txt").write_text("aiosqlite\n")

    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, check=True, capture_output=True)

    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True).stdout.strip()
    return sha


def test_deploy_happy_path(tmp_path):
    """Happy path: успішний деплой релізу, створення symlink current -> releases/<sha>, статус ok."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    sha = init_mock_git_repo(repo_dir)

    app_dir = tmp_path / "app"
    releases_dir = app_dir / "releases"
    current_link = app_dir / "current"
    status_dir = app_dir / "status"
    db_path = tmp_path / "test.db"

    env = os.environ.copy()
    env.update({
        "APP_DIR": str(app_dir),
        "REPO_DIR": str(repo_dir),
        "RELEASES_DIR": str(releases_dir),
        "CURRENT_LINK": str(current_link),
        "STATUS_DIR": str(status_dir),
        "DB_PATH": str(db_path),
        "DRY_RUN": "1",
        "SKIP_FETCH": "1",
        "PYTHON_BIN": sys.executable,
    })

    res = subprocess.run(["bash", DEPLOY_SCRIPT, sha], env=env, capture_output=True, text=True)
    assert res.returncode == 0, f"deploy.sh failed: {res.stdout}\n{res.stderr}"

    # Перевіряємо що реліз розпаковано і symlink встановлено
    target_release = releases_dir / sha
    assert target_release.is_dir()
    assert (target_release / "bot.py").is_file()
    assert current_link.is_symlink()
    assert os.path.realpath(str(current_link)) == os.path.realpath(str(target_release))

    # Перевіряємо статус файл
    status_file = status_dir / f"{sha}.json"
    assert status_file.is_file()
    status_data = json.loads(status_file.read_text())
    assert status_data["status"] == "ok"
    assert status_data["sha"] == sha


def test_deploy_smoke_failure_preserves_symlink(tmp_path):
    """Якщо smoke перевірка падає, symlink НЕ змінюється, статус failed."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    sha = init_mock_git_repo(repo_dir)

    app_dir = tmp_path / "app"
    current_link = app_dir / "current"
    status_dir = app_dir / "status"

    # Створюємо фіктивний попередній реліз
    prev_release = app_dir / "releases" / "old_sha"
    prev_release.mkdir(parents=True)
    (prev_release / "old_marker").write_text("v1")
    current_link.symlink_to(prev_release)

    env = os.environ.copy()
    env.update({
        "APP_DIR": str(app_dir),
        "REPO_DIR": str(repo_dir),
        "CURRENT_LINK": str(current_link),
        "STATUS_DIR": str(status_dir),
        "DRY_RUN": "1",
        "SKIP_FETCH": "1",
        "SIMULATE_SMOKE_FAIL": "1",
        "PYTHON_BIN": sys.executable,
    })

    res = subprocess.run(["bash", DEPLOY_SCRIPT, sha], env=env, capture_output=True, text=True)
    assert res.returncode != 0

    # Symlink залишився на попередньому релізі
    assert current_link.is_symlink()
    assert os.path.realpath(str(current_link)) == os.path.realpath(str(prev_release))

    # Статус failed
    status_file = status_dir / f"{sha}.json"
    assert status_file.is_file()
    status_data = json.loads(status_file.read_text())
    assert status_data["status"] == "failed"


def test_deploy_health_failure_triggers_rollback(tmp_path):
    """Якщо health check падає після switch, symlink автоматично повертається на PREV, статус rolled_back."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    sha = init_mock_git_repo(repo_dir)

    app_dir = tmp_path / "app"
    current_link = app_dir / "current"
    status_dir = app_dir / "status"

    # Попередній реліз
    prev_release = app_dir / "releases" / "previous_working_sha"
    prev_release.mkdir(parents=True)
    (prev_release / "version").write_text("v1")
    current_link.symlink_to(prev_release)

    env = os.environ.copy()
    env.update({
        "APP_DIR": str(app_dir),
        "REPO_DIR": str(repo_dir),
        "CURRENT_LINK": str(current_link),
        "STATUS_DIR": str(status_dir),
        "DRY_RUN": "1",
        "SKIP_FETCH": "1",
        "SIMULATE_HEALTH_FAIL": "1",
        "PYTHON_BIN": sys.executable,
    })

    res = subprocess.run(["bash", DEPLOY_SCRIPT, sha], env=env, capture_output=True, text=True)
    assert res.returncode != 0

    # Symlink був відкочений назад на попередній реліз
    assert current_link.is_symlink()
    assert os.path.realpath(str(current_link)) == os.path.realpath(str(prev_release))

    # Статус rolled_back
    status_file = status_dir / f"{sha}.json"
    assert status_file.is_file()
    status_data = json.loads(status_file.read_text())
    assert status_data["status"] == "rolled_back"


def test_deploy_prunes_old_releases(tmp_path):
    """Pruning видаляє старіші за N релізи, не чіпаючи активний та попередній."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    sha = init_mock_git_repo(repo_dir)

    app_dir = tmp_path / "app"
    releases_dir = app_dir / "releases"
    releases_dir.mkdir(parents=True)

    # Створюємо 8 старих фіктивних релізів
    old_dirs = []
    for i in range(8):
        d = releases_dir / f"rel_old_{i:02d}"
        d.mkdir()
        (d / "file.txt").write_text(str(i))
        old_dirs.append(d)

    current_link = app_dir / "current"
    env = os.environ.copy()
    env.update({
        "APP_DIR": str(app_dir),
        "REPO_DIR": str(repo_dir),
        "RELEASES_DIR": str(releases_dir),
        "CURRENT_LINK": str(current_link),
        "KEEP_RELEASES": "3",
        "DRY_RUN": "1",
        "SKIP_FETCH": "1",
        "PYTHON_BIN": sys.executable,
    })

    res = subprocess.run(["bash", DEPLOY_SCRIPT, sha], env=env, capture_output=True, text=True)
    assert res.returncode == 0

    remaining_dirs = [p for p in releases_dir.iterdir() if p.is_dir()]
    # Новий реліз sha + щонайбільше 3 старих релізи
    assert len(remaining_dirs) <= 4
    assert (releases_dir / sha).is_dir()
