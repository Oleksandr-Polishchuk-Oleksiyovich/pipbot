"""Offline tests for Phase 6 (T-60) documentation acceptance criteria.

Verifies that:
1. All required documentation files exist (README.md, CLOUD_DEPLOYMENT.md, docs/RUNBOOK.md, docs/ARCHITECTURE.md).
2. Forbidden legacy patterns are completely absent:
   - 'User=root'
   - 'update_bot.py'
   - tar-overwrite instructions
   - primary DB path in code directory (/opt/sportnerdbot/challenge.db)
3. docs/RUNBOOK.md covers deploy, rollback, backup, and restore.
4. Relative file references in the documentation point to valid paths on disk.
"""

from pathlib import Path
import re
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

DOC_FILES = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "CLOUD_DEPLOYMENT.md",
    REPO_ROOT / "docs" / "RUNBOOK.md",
    REPO_ROOT / "docs" / "ARCHITECTURE.md",
]


def test_required_docs_exist():
    for doc in DOC_FILES:
        assert doc.is_file(), f"Expected documentation file does not exist: {doc}"
        assert doc.stat().st_size > 100, f"Documentation file is too small/empty: {doc}"


def test_no_forbidden_legacy_references():
    forbidden_patterns = [
        r"User\s*=\s*root",
        r"update_bot\.py",
        r"/opt/sportnerdbot/challenge\.db",
        r"tar\s+-?x.*(?:sportnerdbot|\.tgz)",
    ]

    for doc in DOC_FILES:
        content = doc.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            matches = re.findall(pattern, content, flags=re.IGNORECASE)
            assert not matches, (
                f"Found forbidden pattern '{pattern}' in {doc.name}: {matches}"
            )


def test_docs_reference_non_root_and_var_lib():
    for doc in DOC_FILES:
        content = doc.read_text(encoding="utf-8")
        assert "sportnerd" in content, f"{doc.name} should reference non-root user sportnerd"
        assert "/var/lib/sportnerdbot/challenge.db" in content, (
            f"{doc.name} should reference DB path /var/lib/sportnerdbot/challenge.db"
        )


def test_runbook_covers_critical_operations():
    runbook_path = REPO_ROOT / "docs" / "RUNBOOK.md"
    content = runbook_path.read_text(encoding="utf-8").lower()

    assert "deploy" in content, "Runbook must describe deploy"
    assert "rollback" in content, "Runbook must describe rollback"
    assert "backup" in content, "Runbook must describe backup"
    assert "restore" in content or "відновлення" in content, "Runbook must describe restore"
    assert "/approve" in content, "Runbook must describe /approve command"
    assert "/rollback" in content, "Runbook must describe /rollback command"


def test_docs_referenced_repo_files_exist():
    referenced_files = [
        "deploy/deploy.sh",
        "deploy/README.md",
        "setup_remote.sh",
        "deploy.ps1",
        "gateway/app.py",
        "cv_worker/worker.py",
        "database/migrate.py",
        "skills/admin_dev.md",
        "core/challenge.py",
        "core/signing.py",
        "app/flow.py",
        "app/offload.py",
    ]

    for rel_path in referenced_files:
        full_path = REPO_ROOT / rel_path
        assert full_path.exists(), f"Referenced repository file does not exist: {rel_path}"
