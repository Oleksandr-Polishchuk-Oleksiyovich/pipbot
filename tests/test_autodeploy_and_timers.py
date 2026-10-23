"""
Unit tests for autodeploy, live worker timers, gateway wait override, and VM config sync:
  - sportnerd-autodeploy.sh re-exec on pull
  - vm_config.env non-secret sync (and refusal to overwrite secrets like BOT_TOKEN / DB_PATH)
  - timer OnUnitActiveSec update
  - live reload of config/workers.env in BoxCVWorker and AdminTasksWorker
  - ?wait= override on /cv/claim and /admin_tasks/claim
  - tools/worker_timers.py set/status
  - /autodeploy/trigger and /autodeploy/status endpoints
"""
import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from core.signing import sign_request
from cv_worker.box_worker import BoxCVWorker, GatewayClient
from database.store import Store
from gateway.app import create_app
from tools.admin_tasks import AdminTasksClient
from tools.admin_tasks_worker import AdminTasksWorker
import tools.worker_timers as worker_timers

ROOT_DIR = Path(__file__).resolve().parent.parent
AUTODEPLOY_SCRIPT = str(ROOT_DIR / "deploy" / "autodeploy" / "sportnerd-autodeploy.sh")
SECRET = "test-secret-autodeploy"


def setup_git_repo_with_origin(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)

    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", str(origin), str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True)
    (repo / "init.txt").write_text("init")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=repo, check=True, capture_output=True)
    return origin, repo


# ---------------------------------------------------------------------------
# 1. Tests for sportnerd-autodeploy.sh
# ---------------------------------------------------------------------------

def test_sync_vm_config_and_units_offline(tmp_path):
    """
    Tests:
      - timer OnUnitActiveSec updated according to AUTODEPLOY_INTERVAL in vm_config.env
      - non-secret variables are synced to ENV_FILE
      - protected keys (BOT_TOKEN, DB_PATH, GATEWAY_HMAC_SECRET, etc.) are never overwritten
      - AUTODEPLOY_INTERVAL is not synced into ENV_FILE
      - idempotency on repeat runs
    """
    origin, repo_dir = setup_git_repo_with_origin(tmp_path)
    repo_deploy = repo_dir / "deploy"
    repo_autodeploy = repo_deploy / "autodeploy"
    repo_autodeploy.mkdir(parents=True)

    # Copy template unit files
    (repo_autodeploy / "sportnerd-autodeploy.service").write_text(
        "[Unit]\nDescription=Test\n[Service]\nExecStart=/opt/sportnerdbot/repo/deploy/autodeploy/sportnerd-autodeploy.sh\n"
    )
    (repo_autodeploy / "sportnerd-autodeploy.timer").write_text(
        "[Unit]\nDescription=Test\n[Timer]\nOnUnitActiveSec=1min\n"
    )

    vm_config = repo_deploy / "vm_config.env"
    vm_config.write_text(
        "AUTODEPLOY_INTERVAL=45s\n"
        "CV_CLAIM_WAIT_S=35\n"
        "CV_RESULTS_INTERVAL_SEC=15\n"
        "ADMIN_TASKS_ANNOUNCE_INTERVAL_SEC=90\n"
        "BOT_TOKEN=malicious_bot_token\n"
        "DB_PATH=/tmp/evil.db\n"
        "GATEWAY_HMAC_SECRET=malicious_hmac\n"
        "CUSTOM_NON_SECRET=custom_value\n"
    )

    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "add config and units"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=repo_dir, check=True, capture_output=True)

    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True).stdout.strip()

    current_rev_file = tmp_path / "REVISION"
    current_rev_file.write_text(sha)

    env_file = tmp_path / "env"
    env_file.write_text(
        "BOT_TOKEN=super_secret_bot_token\n"
        "DB_PATH=/var/lib/sportnerdbot/challenge.db\n"
        "GATEWAY_HMAC_SECRET=super_secret_hmac\n"
        "CV_CLAIM_WAIT_S=25\n"
    )

    systemd_dir = tmp_path / "systemd"
    lock_file = tmp_path / "autodeploy.lock"

    env = os.environ.copy()
    env.update({
        "REPO": str(repo_dir),
        "CURRENT_REV_FILE": str(current_rev_file),
        "FAILED_FILE": str(tmp_path / "failed"),
        "LOCK_FILE": str(lock_file),
        "VM_CONFIG_FILE": str(vm_config),
        "ENV_FILE": str(env_file),
        "SYSTEMD_DIR": str(systemd_dir),
        "DRY_RUN": "1",
    })

    # Run sportnerd-autodeploy.sh (REMOTE == DEPLOYED path)
    res = subprocess.run(["bash", AUTODEPLOY_SCRIPT], env=env, capture_output=True, text=True)
    assert res.returncode == 0, f"autodeploy failed: {res.stderr}\n{res.stdout}"

    # 1. Timer OnUnitActiveSec was updated to 45s
    rendered_timer = (systemd_dir / "sportnerd-autodeploy.timer").read_text()
    assert "OnUnitActiveSec=45s" in rendered_timer

    # 2. Service ExecStart was rendered with $REPO
    rendered_svc = (systemd_dir / "sportnerd-autodeploy.service").read_text()
    assert str(repo_dir) in rendered_svc

    # 3. ENV_FILE checks
    env_contents = env_file.read_text()
    # Secrets must NOT be overwritten
    assert "BOT_TOKEN=super_secret_bot_token" in env_contents
    assert "malicious_bot_token" not in env_contents
    assert "DB_PATH=/var/lib/sportnerdbot/challenge.db" in env_contents
    assert "/tmp/evil.db" not in env_contents
    assert "GATEWAY_HMAC_SECRET=super_secret_hmac" in env_contents
    assert "malicious_hmac" not in env_contents
    # Non-secrets updated and added
    assert "CV_CLAIM_WAIT_S=35" in env_contents
    assert "CV_RESULTS_INTERVAL_SEC=15" in env_contents
    assert "ADMIN_TASKS_ANNOUNCE_INTERVAL_SEC=90" in env_contents
    assert "CUSTOM_NON_SECRET=custom_value" in env_contents
    # AUTODEPLOY_INTERVAL must be skipped
    assert "AUTODEPLOY_INTERVAL" not in env_contents

    # 4. Repeat run should succeed idempotently
    res2 = subprocess.run(["bash", AUTODEPLOY_SCRIPT], env=env, capture_output=True, text=True)
    assert res2.returncode == 0


def test_invalid_autodeploy_interval_fallback(tmp_path):
    """Invalid AUTODEPLOY_INTERVAL in vm_config.env falls back to 1min."""
    origin, repo_dir = setup_git_repo_with_origin(tmp_path)
    repo_deploy = repo_dir / "deploy"
    repo_autodeploy = repo_deploy / "autodeploy"
    repo_autodeploy.mkdir(parents=True)

    (repo_autodeploy / "sportnerd-autodeploy.timer").write_text(
        "[Unit]\nDescription=Test\n[Timer]\nOnUnitActiveSec=2min\n"
    )

    vm_config = repo_deploy / "vm_config.env"
    vm_config.write_text("AUTODEPLOY_INTERVAL=invalid_interval_999\n")

    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "add config"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=repo_dir, check=True, capture_output=True)

    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True).stdout.strip()

    current_rev_file = tmp_path / "REVISION"
    current_rev_file.write_text(sha)
    systemd_dir = tmp_path / "systemd"

    env = os.environ.copy()
    env.update({
        "REPO": str(repo_dir),
        "CURRENT_REV_FILE": str(current_rev_file),
        "FAILED_FILE": str(tmp_path / "failed"),
        "LOCK_FILE": str(tmp_path / "lock"),
        "VM_CONFIG_FILE": str(vm_config),
        "ENV_FILE": str(tmp_path / "nonexistent_env"),
        "SYSTEMD_DIR": str(systemd_dir),
        "DRY_RUN": "1",
    })

    res = subprocess.run(["bash", AUTODEPLOY_SCRIPT], env=env, capture_output=True, text=True)
    assert res.returncode == 0
    rendered_timer = (systemd_dir / "sportnerd-autodeploy.timer").read_text()
    assert "OnUnitActiveSec=1min" in rendered_timer


def test_autodeploy_reexec_on_pull(tmp_path):
    """
    Tests sportnerd-autodeploy.sh re-exec on pull:
    - upstream has new commit
    - local repo pulls with --ff-only
    - re-executes with AUTODEPLOY_REEXEC=1
    - runs deploy/deploy.sh
    """
    origin_git, repo_dir = setup_git_repo_with_origin(tmp_path)

    # Prepare deploy script in repo_dir and push to upstream
    deploy_dir = repo_dir / "deploy"
    deploy_autodeploy = deploy_dir / "autodeploy"
    deploy_autodeploy.mkdir(parents=True)

    shutil.copy(AUTODEPLOY_SCRIPT, str(deploy_autodeploy / "sportnerd-autodeploy.sh"))
    os.chmod(str(deploy_autodeploy / "sportnerd-autodeploy.sh"), 0o755)

    deployed_marker = tmp_path / "deployed_commit.txt"
    (deploy_dir / "deploy.sh").write_text(f"""#!/usr/bin/env bash
echo "$1" > "{deployed_marker}"
exit 0
""")
    os.chmod(str(deploy_dir / "deploy.sh"), 0o755)

    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "add deploy scripts"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=repo_dir, check=True, capture_output=True)
    sha_start = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True).stdout.strip()

    # Clone dev repo to make commit 2 and push to origin
    dev_repo = tmp_path / "dev_repo"
    subprocess.run(["git", "clone", str(origin_git), str(dev_repo)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=dev_repo, check=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=dev_repo, check=True)

    (dev_repo / "init.txt").write_text("v2")
    subprocess.run(["git", "add", "."], cwd=dev_repo, check=True)
    subprocess.run(["git", "commit", "-m", "commit 2"], cwd=dev_repo, check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=dev_repo, check=True, capture_output=True)
    sha2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=dev_repo, capture_output=True, text=True, check=True).stdout.strip()
    assert sha2 != sha_start

    current_rev_file = tmp_path / "REVISION"
    current_rev_file.write_text(sha_start)

    env = os.environ.copy()
    env.update({
        "REPO": str(repo_dir),
        "CURRENT_REV_FILE": str(current_rev_file),
        "FAILED_FILE": str(tmp_path / "failed_sha"),
        "LOCK_FILE": str(tmp_path / "autodeploy.lock"),
        "VM_CONFIG_FILE": str(deploy_dir / "vm_config.env"),
        "ENV_FILE": str(tmp_path / "env"),
        "SYSTEMD_DIR": str(tmp_path / "systemd"),
        "DRY_RUN": "1",
    })
    env.pop("AUTODEPLOY_REEXEC", None)

    # Run autodeploy script from repo_dir
    res = subprocess.run(["bash", str(deploy_autodeploy / "sportnerd-autodeploy.sh")], env=env, capture_output=True, text=True)
    assert res.returncode == 0, f"Script failed: {res.stderr}\n{res.stdout}"
    assert f"deploying {sha2[:7]}" in res.stdout
    assert f"autodeploy {sha2[:7]} ok" in res.stdout
    assert deployed_marker.is_file()
    assert "origin/main" in deployed_marker.read_text()


# ---------------------------------------------------------------------------
# 2. Tests for BoxCVWorker & AdminTasksWorker live reload of config/workers.env
# ---------------------------------------------------------------------------

def test_box_cv_worker_live_reload_workers_env(tmp_path, monkeypatch):
    """BoxCVWorker re-reads config/workers.env each iteration and sleeps on 204 if CV_CHECK_INTERVAL_SEC > 0."""
    workers_file = tmp_path / "workers.env"
    workers_file.write_text(
        "CV_POLL_WAIT_SEC=15\n"
        "CV_CHECK_INTERVAL_SEC=0\n"
        "CV_HEARTBEAT_S=300\n"
    )
    monkeypatch.setenv("WORKERS_ENV_FILE", str(workers_file))

    claim_calls = []
    class FakeClient:
        def claim(self, wait_s=None):
            claim_calls.append(wait_s)
            return None  # 204 idle

    sleeps = []
    worker = BoxCVWorker(FakeClient(), sleep=sleeps.append)

    # Iteration 1: CV_CHECK_INTERVAL_SEC == 0 -> no sleep on 204
    assert worker.step() is False
    assert claim_calls == [15.0]
    assert sleeps == []

    # Live update config/workers.env
    workers_file.write_text(
        "CV_POLL_WAIT_SEC=30\n"
        "CV_CHECK_INTERVAL_SEC=5.5\n"
        "CV_HEARTBEAT_S=300\n"
    )

    # Iteration 2: Re-read config -> wait_s=30, sleeps 5.5s on 204
    assert worker.step() is False
    assert claim_calls == [15.0, 30.0]
    assert sleeps == [5.5]


def test_admin_tasks_worker_live_reload_workers_env(tmp_path, monkeypatch):
    """AdminTasksWorker re-reads config/workers.env each iteration and sleeps on 204 if ADMIN_TASKS_CHECK_INTERVAL_SEC > 0."""
    workers_file = tmp_path / "workers.env"
    workers_file.write_text(
        "ADMIN_TASKS_POLL_WAIT_SEC=20\n"
        "ADMIN_TASKS_CHECK_INTERVAL_SEC=0\n"
        "ADMIN_TASKS_HEARTBEAT_S=300\n"
    )
    monkeypatch.setenv("WORKERS_ENV_FILE", str(workers_file))

    claim_calls = []
    class FakeClient:
        def claim(self, wait_s=None):
            claim_calls.append(wait_s)
            return None  # 204

    sleeps = []
    worker = AdminTasksWorker(FakeClient(), get_config=lambda: {"url": "http://hook", "header": "H", "value": "V"},
                              sleep=sleeps.append)

    # Iteration 1: 0 check interval -> no sleep
    assert worker.step() is False
    assert claim_calls == [20.0]
    assert sleeps == []

    # Live update config/workers.env
    workers_file.write_text(
        "ADMIN_TASKS_POLL_WAIT_SEC=45\n"
        "ADMIN_TASKS_CHECK_INTERVAL_SEC=3\n"
        "ADMIN_TASKS_HEARTBEAT_S=300\n"
    )

    # Iteration 2: re-reads config -> wait_s=45, sleeps 3s on 204
    assert worker.step() is False
    assert claim_calls == [20.0, 45.0]
    assert sleeps == [3.0]


# ---------------------------------------------------------------------------
# 3. Tests for gateway ?wait= override on /cv/claim and /admin_tasks/claim
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gateway_claim_wait_override(tmp_path):
    """
    Tests optional ?wait=<float> override clamped to 0.0 <= wait <= 120.0 on:
      - /cv/claim
      - /admin_tasks/claim
    """
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()

    # Configure gateway with default claim_wait_s=5.0
    app = create_app(store=store, secret=SECRET, cv_options={"claim_wait_s": 5.0})
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        # 1. /cv/claim with wait=0.0 returns immediately (204)
        t0 = time.monotonic()
        headers = sign_request("POST", "/cv/claim", b"", SECRET)
        resp = await client.post("/cv/claim?wait=0.0", headers=headers)
        elapsed = time.monotonic() - t0
        assert resp.status == 204
        assert elapsed < 0.5  # did not wait 5 seconds

        # 2. /admin_tasks/claim with wait=0.0 returns immediately (204)
        t0 = time.monotonic()
        headers = sign_request("GET", "/admin_tasks/claim", b"", SECRET)
        resp = await client.get("/admin_tasks/claim?wait=0.0", headers=headers)
        elapsed = time.monotonic() - t0
        assert resp.status == 204
        assert elapsed < 0.5

        # 3. Negative wait is clamped to 0.0
        t0 = time.monotonic()
        headers = sign_request("POST", "/cv/claim", b"", SECRET)
        resp = await client.post("/cv/claim?wait=-10", headers=headers)
        elapsed = time.monotonic() - t0
        assert resp.status == 204
        assert elapsed < 0.5

    finally:
        await client.close()


# ---------------------------------------------------------------------------
# 4. Tests for /autodeploy/status and /autodeploy/trigger endpoints
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_autodeploy_status_and_trigger(tmp_path):
    """
    Tests HMAC-authenticated endpoints:
      - GET /autodeploy/status
      - POST /autodeploy/trigger
    """
    rev_file = tmp_path / "REVISION"
    rev_file.write_text("1234567890abcdef\n")
    failed_file = tmp_path / "failed_sha"
    failed_file.write_text("fedcba0987654321\n")

    triggered = []
    def fake_trigger():
        triggered.append(True)

    store = Store(str(tmp_path / "t.db"))
    store.init_schema()

    from gateway.admin_tasks_api import AUTODEPLOY_FAILED_FILE_KEY, AUTODEPLOY_REV_FILE_KEY, AUTODEPLOY_TRIGGER_CMD_KEY
    app = create_app(store=store, secret=SECRET)
    app[AUTODEPLOY_REV_FILE_KEY] = str(rev_file)
    app[AUTODEPLOY_FAILED_FILE_KEY] = str(failed_file)
    app[AUTODEPLOY_TRIGGER_CMD_KEY] = fake_trigger

    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        # 1. Unauthenticated requests get 401
        r = await client.get("/autodeploy/status")
        assert r.status == 401

        r = await client.post("/autodeploy/trigger")
        assert r.status == 401

        # 2. Authenticated GET /autodeploy/status
        headers = sign_request("GET", "/autodeploy/status", b"", SECRET)
        r = await client.get("/autodeploy/status", headers=headers)
        assert r.status == 200
        data = await r.json()
        assert data["deployed_sha"] == "1234567890abcdef"
        assert data["failed_sha"] == "fedcba0987654321"

        # 3. Authenticated POST /autodeploy/trigger
        headers = sign_request("POST", "/autodeploy/trigger", b"{}", SECRET)
        r = await client.post("/autodeploy/trigger", data=b"{}", headers=headers)
        assert r.status == 202
        data = await r.json()
        assert data["status"] == "triggered"
        assert data["deployed_sha"] == "1234567890abcdef"
        assert triggered == [True]

    finally:
        await client.close()


# ---------------------------------------------------------------------------
# 5. Tests for tools/worker_timers.py CLI and helpers
# ---------------------------------------------------------------------------

def test_worker_timers_cli_set_and_status(tmp_path, capsys):
    """Tests worker_timers.py 'set' and 'status' subcommands."""
    workers_file = tmp_path / "workers.env"
    vm_file = tmp_path / "vm_config.env"

    workers_file.write_text("CV_POLL_WAIT_SEC=25\n")
    vm_file.write_text("AUTODEPLOY_INTERVAL=1min\n")

    # Run set command
    rc = worker_timers.main([
        "set",
        "--workers-file", str(workers_file),
        "--vm-config-file", str(vm_file),
        "--cv-wait", "18",
        "--cv-interval", "2",
        "--cv-heartbeat", "120",
        "--admin-wait", "22",
        "--admin-interval", "1.5",
        "--admin-heartbeat", "240",
        "--autodeploy-interval", "30s",
        "--cv-claim-wait", "18",
        "--cv-results-interval", "12",
        "--admin-announce-interval", "75",
    ])
    assert rc == 0
    capsys.readouterr()

    # Verify workers_file
    workers_cfg = worker_timers.read_workers_config(str(workers_file))
    assert workers_cfg["CV_POLL_WAIT_SEC"] == 18.0
    assert workers_cfg["CV_CHECK_INTERVAL_SEC"] == 2.0
    assert workers_cfg["CV_HEARTBEAT_S"] == 120.0
    assert workers_cfg["ADMIN_TASKS_POLL_WAIT_SEC"] == 22.0
    assert workers_cfg["ADMIN_TASKS_CHECK_INTERVAL_SEC"] == 1.5
    assert workers_cfg["ADMIN_TASKS_HEARTBEAT_S"] == 240.0

    # Verify vm_file
    vm_cfg = worker_timers.read_vm_config(str(vm_file))
    assert vm_cfg["AUTODEPLOY_INTERVAL"] == "30s"
    assert vm_cfg["CV_CLAIM_WAIT_S"] == "18"
    assert vm_cfg["CV_RESULTS_INTERVAL_SEC"] == "12"
    assert vm_cfg["ADMIN_TASKS_ANNOUNCE_INTERVAL_SEC"] == "75"

    # Run status command
    rc = worker_timers.main([
        "status",
        "--workers-file", str(workers_file),
        "--vm-config-file", str(vm_file),
    ])
    assert rc == 0
    captured = capsys.readouterr()
    status_data = json.loads(captured.out)
    assert status_data["workers_env"]["CV_POLL_WAIT_SEC"] == "18"
    assert status_data["vm_config_env"]["AUTODEPLOY_INTERVAL"] == "30s"

    # Invalid interval rejection
    with pytest.raises(SystemExit):
        worker_timers.main([
            "set",
            "--vm-config-file", str(vm_file),
            "--autodeploy-interval", "invalid_val",
        ])
