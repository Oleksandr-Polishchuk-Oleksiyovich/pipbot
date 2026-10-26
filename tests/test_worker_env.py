"""Box-воркери самодостатні: .env підхоплюється незалежно від середовища викликача; config-помилка -> exit 78 без crash-loop."""
import os
import subprocess
import sys

import pytest

from cv_worker import box_worker

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_load_env_sets_missing_gateway_url_even_if_secret_present(tmp_path, monkeypatch):
    envf = tmp_path / ".env"
    envf.write_text('CV_GATEWAY_URL=https://vm.example:1\nGATEWAY_HMAC_SECRET=from-file\n')
    monkeypatch.setenv("CV_ENV_FILE", str(envf))
    monkeypatch.delenv("CV_GATEWAY_URL", raising=False)
    monkeypatch.setenv("GATEWAY_HMAC_SECRET", "from-env")  # секрет є, URL нема (старий баг)
    box_worker.load_env()
    assert os.environ["CV_GATEWAY_URL"] == "https://vm.example:1"
    assert os.environ["GATEWAY_HMAC_SECRET"] == "from-env"  # env має пріоритет
    monkeypatch.delenv("CV_GATEWAY_URL", raising=False)


def test_main_exits_78_without_gateway_url(tmp_path, monkeypatch):
    monkeypatch.setenv("CV_ENV_FILE", str(tmp_path / "nope.env"))
    monkeypatch.delenv("CV_GATEWAY_URL", raising=False)
    with pytest.raises(SystemExit) as e:
        box_worker.main()
    assert e.value.code == box_worker.EX_CONFIG == 78


@pytest.mark.parametrize("script", ["cv_worker/run_box_worker.sh", "tools/run_admin_tasks_worker.sh"])
def test_supervisor_stops_on_config_error(tmp_path, script):
    env = {k: v for k, v in os.environ.items() if k not in ("CV_GATEWAY_URL", "GATEWAY_HMAC_SECRET")}
    env.update(CV_ENV_FILE=str(tmp_path / "nope.env"), CV_HEAVY="0", GATEWAY_HMAC_SECRET="x",
               CV_PIDFILE=str(tmp_path / "a.pid"), CV_LOG=str(tmp_path / "a.log"),
               ADMIN_TASKS_PIDFILE=str(tmp_path / "b.pid"), ADMIN_TASKS_LOG=str(tmp_path / "b.log"),
               CV_PYTHON=sys.executable, ADMIN_TASKS_PYTHON=sys.executable)
    r = subprocess.run(["bash", script], cwd=REPO, env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 78
    assert "CONFIG ERROR" in r.stderr
    assert not (tmp_path / "a.pid").exists() and not (tmp_path / "b.pid").exists()
