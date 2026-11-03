"""
Tests for cv_worker.autoupdate and worker integration.
Covers:
- no change
- ff update True
- dirty tree skip
- diverged skip
- requirements change triggers install (mock uv)
- disabled via env
- interval gating
- box_worker loop exits after update and never checks during a job
"""
import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cv_worker.autoupdate import Autoupdater, check_and_update
from cv_worker.box_worker import BoxCVWorker, GatewayClient
from tools.admin_tasks_worker import AdminTasksWorker


@pytest.fixture
def git_repos(tmp_path):
    bare_origin = tmp_path / "origin.git"
    dev_repo = tmp_path / "dev"
    work_repo = tmp_path / "work"

    # 1. Bare origin
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_origin)], check=True, capture_output=True)

    # 2. Seed via dev repo
    subprocess.run(["git", "clone", str(bare_origin), str(dev_repo)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(dev_repo), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(dev_repo), check=True)

    (dev_repo / "requirements.txt").write_text("pkg-a==1.0.0\n", encoding="utf-8")
    (dev_repo / "requirements-cv.txt").write_text("pkg-cv==1.0.0\n", encoding="utf-8")
    (dev_repo / "main.py").write_text("print('version 1')\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=str(dev_repo), check=True)
    subprocess.run(["git", "commit", "-m", "v1 commit"], cwd=str(dev_repo), check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=str(dev_repo), check=True)

    # 3. Work repo (simulates box worker checkout)
    subprocess.run(["git", "clone", str(bare_origin), str(work_repo)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(work_repo), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(work_repo), check=True)

    return {
        "origin": bare_origin,
        "dev": dev_repo,
        "work": work_repo,
    }


def test_autoupdate_no_change(git_repos, caplog):
    logger = logging.getLogger("test_no_change")
    caplog.set_level(logging.INFO)
    work = git_repos["work"]

    updated = check_and_update(work, logger)
    assert updated is False
    assert "updated" not in caplog.text


def test_autoupdate_ff_update_true(git_repos, caplog):
    logger = logging.getLogger("test_ff_update")
    caplog.set_level(logging.INFO)
    dev = git_repos["dev"]
    work = git_repos["work"]

    # Push a new commit from dev
    (dev / "main.py").write_text("print('version 2')\n", encoding="utf-8")
    subprocess.run(["git", "add", "main.py"], cwd=str(dev), check=True)
    subprocess.run(["git", "commit", "-m", "v2 commit"], cwd=str(dev), check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=str(dev), check=True)

    dev_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(dev), text=True).strip()

    updated = check_and_update(work, logger)
    assert updated is True

    work_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(work), text=True).strip()
    assert work_head == dev_head
    assert (work / "main.py").read_text(encoding="utf-8") == "print('version 2')\n"
    assert "autoupdate: updated" in caplog.text


def test_autoupdate_dirty_tree_skip(git_repos, caplog):
    logger = logging.getLogger("test_dirty")
    caplog.set_level(logging.WARNING)
    dev = git_repos["dev"]
    work = git_repos["work"]

    # Push a new commit from dev
    (dev / "main.py").write_text("print('version 2')\n", encoding="utf-8")
    subprocess.run(["git", "add", "main.py"], cwd=str(dev), check=True)
    subprocess.run(["git", "commit", "-m", "v2 commit"], cwd=str(dev), check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=str(dev), check=True)

    work_head_before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(work), text=True).strip()

    # Dirty tracked file in work repo
    (work / "main.py").write_text("print('dirty local modification')\n", encoding="utf-8")

    updated = check_and_update(work, logger)
    assert updated is False

    work_head_after = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(work), text=True).strip()
    assert work_head_after == work_head_before
    assert "dirty working tree" in caplog.text


def test_autoupdate_diverged_skip(git_repos, caplog):
    logger = logging.getLogger("test_diverged")
    caplog.set_level(logging.WARNING)
    dev = git_repos["dev"]
    work = git_repos["work"]

    # Local commit in work repo (not pushed)
    (work / "local_file.txt").write_text("local only\n", encoding="utf-8")
    subprocess.run(["git", "add", "local_file.txt"], cwd=str(work), check=True)
    subprocess.run(["git", "commit", "-m", "local commit in work"], cwd=str(work), check=True)
    work_head_before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(work), text=True).strip()

    # Remote commit in dev repo
    (dev / "main.py").write_text("print('remote version 2')\n", encoding="utf-8")
    subprocess.run(["git", "add", "main.py"], cwd=str(dev), check=True)
    subprocess.run(["git", "commit", "-m", "remote commit in dev"], cwd=str(dev), check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=str(dev), check=True)

    updated = check_and_update(work, logger)
    assert updated is False

    work_head_after = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(work), text=True).strip()
    assert work_head_after == work_head_before
    assert "not ancestor" in caplog.text


def test_autoupdate_requirements_change_triggers_install(git_repos, caplog):
    logger = logging.getLogger("test_reqs")
    caplog.set_level(logging.INFO)
    dev = git_repos["dev"]
    work = git_repos["work"]

    # Modify requirements-cv.txt in dev and push
    (dev / "requirements-cv.txt").write_text("pkg-cv==2.0.0\nnew-dep==1.0.0\n", encoding="utf-8")
    subprocess.run(["git", "add", "requirements-cv.txt"], cwd=str(dev), check=True)
    subprocess.run(["git", "commit", "-m", "update requirements-cv"], cwd=str(dev), check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=str(dev), check=True)

    uv_calls = []

    def mock_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "uv":
            uv_calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, stdout="mock uv installed", stderr="")
        return subprocess.run(cmd, *args, **kwargs)

    updated = check_and_update(work, logger, run=mock_run)
    assert updated is True
    assert len(uv_calls) == 1
    assert uv_calls[0][0] == ["uv", "pip", "install", "--python", sys.executable, "-r", "requirements-cv.txt"]
    assert "autoupdate: updated" in caplog.text


def test_autoupdate_requirements_change_uv_failure_continues(git_repos, caplog):
    logger = logging.getLogger("test_reqs_fail")
    caplog.set_level(logging.WARNING)
    dev = git_repos["dev"]
    work = git_repos["work"]

    (dev / "requirements.txt").write_text("pkg-a==2.0.0\n", encoding="utf-8")
    subprocess.run(["git", "add", "requirements.txt"], cwd=str(dev), check=True)
    subprocess.run(["git", "commit", "-m", "update requirements"], cwd=str(dev), check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=str(dev), check=True)

    uv_calls = []

    def mock_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "uv":
            uv_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="network error")
        return subprocess.run(cmd, *args, **kwargs)

    # uv failure should be logged as warning, but check_and_update still returns True
    updated = check_and_update(work, logger, run=mock_run)
    assert updated is True
    assert len(uv_calls) == 1
    assert "uv pip install failed" in caplog.text


def test_autoupdate_git_errors_and_timeouts_caught(git_repos, caplog):
    logger = logging.getLogger("test_errors")
    work = git_repos["work"]

    def mock_timeout(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    assert check_and_update(work, logger, run=mock_timeout) is False
    assert "git error or timeout" in caplog.text


def test_autoupdate_disabled_via_env(git_repos, monkeypatch):
    logger = logging.getLogger("test_disabled")
    work = git_repos["work"]
    dev = git_repos["dev"]

    (dev / "main.py").write_text("print('version 2')\n", encoding="utf-8")
    subprocess.run(["git", "add", "main.py"], cwd=str(dev), check=True)
    subprocess.run(["git", "commit", "-m", "v2 commit"], cwd=str(dev), check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=str(dev), check=True)

    monkeypatch.setenv("BOX_AUTOUPDATE", "0")

    mock_check = MagicMock()
    updater = Autoupdater(repo_dir=str(work), logger=logger, check_fn=mock_check)
    res = updater.maybe_update(now=100.0)

    assert res is False
    mock_check.assert_not_called()


def test_autoupdate_interval_gating(git_repos, monkeypatch):
    logger = logging.getLogger("test_interval")
    work = git_repos["work"]

    monkeypatch.setenv("BOX_AUTOUPDATE", "1")
    monkeypatch.setenv("BOX_AUTOUPDATE_INTERVAL", "60")

    mock_check = MagicMock(return_value=False)
    updater = Autoupdater(repo_dir=str(work), logger=logger, check_fn=mock_check)

    # First call at t=100.0 -> runs check
    assert updater.maybe_update(100.0) is False
    assert mock_check.call_count == 1

    # Second call at t=120.0 (20s later < 60s) -> skipped
    assert updater.maybe_update(120.0) is False
    assert mock_check.call_count == 1

    # Call at t=159.9 (< 60s) -> skipped
    assert updater.maybe_update(159.9) is False
    assert mock_check.call_count == 1

    # Call at t=160.0 (>= 60s) -> runs check
    assert updater.maybe_update(160.0) is False
    assert mock_check.call_count == 2


def test_box_worker_loop_exits_after_update_and_never_during_job():
    """
    Verify:
    1. autoupdater.maybe_update() is NEVER called during process()
    2. only after step() returns between jobs, autoupdater is called
    3. if autoupdater returns True, worker exits cleanly with exit code 0
    """
    mock_client = MagicMock(spec=GatewayClient)
    # Return one job, then None
    job_payload = {
        "request_id": "test-req-12345",
        "claim_token": "token-xyz",
        "video_url": "/test.mp4",
    }
    mock_client.claim.side_effect = [job_payload, None]
    mock_client.post_result.return_value = 200

    autoupdate_calls_during_process = []
    mock_autoupdater = MagicMock(spec=Autoupdater)

    def fake_analyzer(video_path):
        # Record whether maybe_update was called while process() was executing
        autoupdate_calls_during_process.append(mock_autoupdater.maybe_update.call_count)
        return {"reps": 10}

    # Worker configured with autoupdater returning True
    mock_autoupdater.maybe_update.return_value = True

    worker = BoxCVWorker(
        client=mock_client,
        analyzer=fake_analyzer,
        autoupdater=mock_autoupdater,
        get_workers_config=lambda: {"CV_POLL_WAIT_SEC": 0.0, "CV_CHECK_INTERVAL_SEC": 0.0, "CV_HEARTBEAT_S": 0.0},
    )

    with pytest.raises(SystemExit) as exc_info:
        worker.run()

    # Exit code must be 0
    assert exc_info.value.code == 0

    # During process(), maybe_update was called 0 times
    assert autoupdate_calls_during_process == [0]

    # After step() completed the job, maybe_update was called
    assert mock_autoupdater.maybe_update.call_count == 1


def test_box_worker_loop_exits_on_idle_timeout_update():
    """Verify that when worker is idle (step() returns False), update also triggers clean exit rc=0."""
    mock_client = MagicMock(spec=GatewayClient)
    mock_client.claim.return_value = None  # 204 timeout

    mock_autoupdater = MagicMock(spec=Autoupdater)
    mock_autoupdater.maybe_update.return_value = True

    worker = BoxCVWorker(
        client=mock_client,
        autoupdater=mock_autoupdater,
        get_workers_config=lambda: {"CV_POLL_WAIT_SEC": 0.0, "CV_CHECK_INTERVAL_SEC": 0.0, "CV_HEARTBEAT_S": 0.0},
    )

    with pytest.raises(SystemExit) as exc_info:
        worker.run()

    assert exc_info.value.code == 0
    assert mock_autoupdater.maybe_update.call_count == 1


def test_admin_tasks_worker_loop_exits_after_update():
    """Verify admin_tasks_worker also checks autoupdate between tasks and exits cleanly rc=0."""
    mock_client = MagicMock()
    mock_client.claim.return_value = None  # idle

    mock_autoupdater = MagicMock(spec=Autoupdater)
    mock_autoupdater.maybe_update.return_value = True

    worker = AdminTasksWorker(
        client=mock_client,
        get_config=lambda: {"url": "http://mock", "header": "Authorization", "value": "Bearer x"},
        autoupdater=mock_autoupdater,
        get_workers_config=lambda: {"ADMIN_TASKS_POLL_WAIT_SEC": 0.0, "ADMIN_TASKS_CHECK_INTERVAL_SEC": 0.0, "ADMIN_TASKS_HEARTBEAT_S": 0.0},
    )

    with pytest.raises(SystemExit) as exc_info:
        worker.run()

    assert exc_info.value.code == 0
    assert mock_autoupdater.maybe_update.call_count == 1
