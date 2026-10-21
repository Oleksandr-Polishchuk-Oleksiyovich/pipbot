"""
Offline tests for production DB_PATH safety guardrails:
- core/config.py: DB_PATH must be set explicitly and must live outside the code/release
  directory whenever the process is considered "production" (APP_ENV=production).
- core/challenge._active_users: no silent auto-seeding of default participants in production.
"""
import os
import subprocess
import sys

from core import challenge
from database.store import Store

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _import_config(env_overrides):
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", "import core.config"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_config_production_requires_explicit_db_path():
    env = {"APP_ENV": "production"}
    env.pop("DB_PATH", None)
    res = _import_config(env)
    assert res.returncode != 0
    assert "DB_PATH is not set in production" in res.stderr


def test_config_production_rejects_db_path_inside_release_dir():
    res = _import_config({"APP_ENV": "production", "DB_PATH": os.path.join(REPO_ROOT, "challenge.db")})
    assert res.returncode != 0
    assert "resolves inside the code/release directory" in res.stderr


def test_config_production_accepts_db_path_outside_release_dir(tmp_path):
    db_path = tmp_path / "var" / "challenge.db"
    res = _import_config({"APP_ENV": "production", "DB_PATH": str(db_path)})
    assert res.returncode == 0, res.stderr


def test_config_dev_mode_defaults_to_local_challenge_db():
    env = {}
    env.pop("APP_ENV", None)
    env.pop("DB_PATH", None)
    res = _import_config(env)
    assert res.returncode == 0, res.stderr


def test_active_users_does_not_seed_in_production(tmp_path, monkeypatch):
    monkeypatch.setattr(challenge, "IS_PRODUCTION", True)
    store = Store(str(tmp_path / "empty_prod.db"))
    store.init_schema()
    assert store.list_users() == []

    users = challenge._active_users(store)

    assert users == []
    assert store.list_users() == []  # ніякого автосіду


def test_active_users_seeds_in_dev_as_before(tmp_path, monkeypatch):
    monkeypatch.setattr(challenge, "IS_PRODUCTION", False)
    store = Store(str(tmp_path / "empty_dev.db"))
    store.init_schema()
    assert store.list_users() == []

    users = challenge._active_users(store)

    assert {u.slug for u in users} == {"lyosha", "sasha"}
