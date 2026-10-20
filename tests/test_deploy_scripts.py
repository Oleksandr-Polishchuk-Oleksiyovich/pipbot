"""
Offline tests for deploy artifacts cleanup and safety (T-23).
Acceptance criteria:
- repo does not contain update_bot.py
- grep does not find copying of .env in deploy scripts
- ad-hoc migration scripts are not invoked unconditionally in deploy scripts
- setup_remote.sh sets up non-root user sportnerd and release-dir structure
- bash -n passes for all shell scripts
"""
import os
import re
import subprocess
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent


def test_update_bot_removed():
    """update_bot.py має бути повністю видалено з репозиторію."""
    update_bot_path = ROOT_DIR / "update_bot.py"
    assert not update_bot_path.exists(), "update_bot.py must be removed"


def test_no_env_copying_in_deploy_scripts():
    """Grep не знаходить копіювання .env у deploy-скриптах (deploy.ps1, setup_remote.sh, deploy/deploy.sh)."""
    scripts = [
        ROOT_DIR / "deploy.ps1",
        ROOT_DIR / "setup_remote.sh",
        ROOT_DIR / "deploy" / "deploy.sh",
    ]

    env_copy_patterns = [
        r"cp\s+.*\.env",
        r"tar\s+.*\.env",
        r"scp\s+.*\.env",
        r"rsync\s+.*\.env",
    ]

    for script in scripts:
        assert script.exists(), f"Expected script {script} to exist"
        content = script.read_text(encoding="utf-8")
        for line in content.splitlines():
            # Ігноруємо коментарі
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("<#") or stripped.startswith("."):
                continue
            for pat in env_copy_patterns:
                assert not re.search(pat, line), f"Found forbidden .env copy pattern '{pat}' in {script.name}: {line}"


def test_no_unconditional_adhoc_migrations_in_deploy_scripts():
    """Ад-хок міграційні скрипти (migrate_prompts, migrate_users, enable_alternating) не викликаються в deploy-скриптах."""
    scripts = [
        ROOT_DIR / "deploy.ps1",
        ROOT_DIR / "setup_remote.sh",
        ROOT_DIR / "deploy" / "deploy.sh",
    ]

    forbidden_scripts = [
        "migrate_prompts.py",
        "migrate_users.py",
        "enable_alternating.py",
    ]

    for script in scripts:
        content = script.read_text(encoding="utf-8")
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("<#") or stripped.startswith("."):
                continue
            for adhoc in forbidden_scripts:
                assert adhoc not in line, f"Found forbidden ad-hoc migration call '{adhoc}' in {script.name}: {line}"


def test_setup_remote_uses_non_root_user():
    """setup_remote.sh створює non-root користувача sportnerd і конфігурує User=sportnerd."""
    setup_script = ROOT_DIR / "setup_remote.sh"
    content = setup_script.read_text(encoding="utf-8")

    assert "sportnerd" in content
    assert "useradd" in content
    assert "User=root" not in content


def test_systemd_unit_templates_use_non_root():
    """Усі unit-шаблони в deploy/systemd/ використовують User=sportnerd і NoNewPrivileges=yes."""
    systemd_dir = ROOT_DIR / "deploy" / "systemd"
    assert systemd_dir.is_dir()

    units = list(systemd_dir.glob("*.service"))
    assert len(units) >= 3

    for unit in units:
        content = unit.read_text(encoding="utf-8")
        assert "User=sportnerd" in content
        assert "User=root" not in content
        if unit.name != "sportnerdbot-deploy.service":  # oneshot
            assert "NoNewPrivileges=yes" in content
            assert "ProtectSystem=strict" in content


def test_shell_scripts_syntax():
    """Усі bash скрипти проходять перевірку bash -n."""
    shell_scripts = [
        ROOT_DIR / "setup_remote.sh",
        ROOT_DIR / "deploy" / "deploy.sh",
    ]
    for s in shell_scripts:
        res = subprocess.run(["bash", "-n", str(s)], capture_output=True, text=True)
        assert res.returncode == 0, f"bash -n failed on {s}: {res.stderr}"
