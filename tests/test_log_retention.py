"""
Offline tests for 7-day log retention:
- Store.prune_old_logs deletes rows older than the cutoff from "noisy" technical bookkeeping
  tables (cv_jobs, offload_requests) but NEVER touches chat_history or video_checks (challenge
  data: chat/report history) nor admin_audit/prompt_history (audit trail / prompt version history).
- ChatFlow wires pruning into init_storage() (startup) and prune_old_logs() (daily job).
- Admin entry points (admin_quick / admin_dev) log user id/name, trigger and allowed/denied.
"""
import logging
from datetime import timedelta

import pytest

from app.flow import ChatFlow
from agents.router import RouteDecision
from core.config import LOG_RETENTION_DAYS
from database.store import ChatMessage, Store
from migrate_users import ensure_default_users
from tests.flow_fixtures import CHAT, Clock, FakeSender, NOW, SON


OLD_TS = "2026-09-01 00:00:00"
NEW_TS = "2026-10-20 00:00:00"


def _seed_log_rows(store: Store, created_at: str, suffix: str) -> None:
    with store._connect() as con:
        con.execute(
            "INSERT INTO chat_history (chat_id, user_name, role, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (CHAT, "User", "user", f"msg-{suffix}", created_at),
        )
        con.execute(
            "INSERT INTO video_checks (request_id, chat_id, message_id, created_at) VALUES (?, ?, ?, ?)",
            (f"vid-{suffix}", CHAT, 1, created_at),
        )
        con.execute(
            "INSERT INTO cv_jobs (request_id, status, created_at, updated_at) VALUES (?, 'queued', ?, ?)",
            (f"cv-{suffix}", created_at, created_at),
        )
        con.execute(
            "INSERT INTO offload_requests (request_id, type, status, created_at, updated_at) "
            "VALUES (?, 'admin', 'sent', ?, ?)",
            (f"off-{suffix}", created_at, created_at),
        )
        con.execute(
            "INSERT INTO admin_audit (request_id, status, created_at, updated_at) VALUES (?, 'planned', ?, ?)",
            (f"audit-{suffix}", created_at, created_at),
        )
        con.execute(
            "INSERT INTO prompt_history (key, content, version, created_at) VALUES ('admin', 'x', 1, ?)",
            (created_at,),
        )


def test_prune_old_logs_deletes_old_rows_keeps_recent(tmp_path):
    store = Store(str(tmp_path / "retention.db"))
    store.init_schema()
    _seed_log_rows(store, OLD_TS, "old")
    _seed_log_rows(store, NEW_TS, "new")

    cutoff = "2026-10-01 00:00:00"
    deleted = store.prune_old_logs(cutoff)

    assert deleted == {"cv_jobs": 1, "offload_requests": 1}

    with store._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM cv_jobs").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM offload_requests").fetchone()[0] == 1
        # chat_history/video_checks = challenge data (chat/report history) — NEVER pruned
        assert con.execute("SELECT COUNT(*) FROM chat_history").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM video_checks").fetchone()[0] == 2
        # admin_audit/prompt_history NEVER pruned, both old+new rows survive
        assert con.execute("SELECT COUNT(*) FROM admin_audit").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM prompt_history").fetchone()[0] == 2

    remaining_chat = store._connect().execute("SELECT text FROM chat_history ORDER BY text").fetchall()
    assert remaining_chat == [("msg-new",), ("msg-old",)]


def test_init_storage_prunes_old_logs_on_startup(tmp_path):
    store = Store(str(tmp_path / "startup_prune.db"))
    store.init_schema()
    ensure_default_users(store)

    clock = Clock(NOW)  # 2026-10-04
    old_ts = "2026-09-01 00:00:00"  # > LOG_RETENTION_DAYS (7) before NOW
    _seed_log_rows(store, old_ts, "old")

    flow = ChatFlow(store, llm=None, sender=FakeSender(), now=clock)
    flow.init_storage()

    with store._connect() as con:
        # cv_jobs/offload_requests = technical bookkeeping — pruned once older than cutoff
        assert con.execute("SELECT COUNT(*) FROM cv_jobs WHERE request_id='cv-old'").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM offload_requests WHERE request_id='off-old'").fetchone()[0] == 0
        # chat_history/video_checks = challenge data — survives even though it's older than the cutoff
        assert con.execute("SELECT COUNT(*) FROM chat_history WHERE text='msg-old'").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM video_checks WHERE request_id='vid-old'").fetchone()[0] == 1
        # admin_audit survives even though it's older than the cutoff
        assert con.execute("SELECT COUNT(*) FROM admin_audit WHERE request_id='audit-old'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_daily_prune_old_logs_job(tmp_path):
    store = Store(str(tmp_path / "daily_prune.db"))
    store.init_schema()
    ensure_default_users(store)
    _seed_log_rows(store, "2026-09-01 00:00:00", "old")
    _seed_log_rows(store, NOW.strftime("%Y-%m-%d %H:%M:%S"), "new")

    flow = ChatFlow(store, llm=None, sender=FakeSender(), now=Clock(NOW))
    deleted = await flow.prune_old_logs()

    assert deleted["cv_jobs"] == 1
    assert deleted["offload_requests"] == 1
    assert "chat_history" not in deleted
    assert "video_checks" not in deleted

    with store._connect() as con:
        # challenge data (old + new chat/report rows) is untouched by the daily job
        assert con.execute("SELECT COUNT(*) FROM chat_history").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM video_checks").fetchone()[0] == 2


def test_log_retention_cutoff_uses_config_days(tmp_path):
    store = Store(str(tmp_path / "cutoff.db"))
    store.init_schema()
    flow = ChatFlow(store, llm=None, sender=FakeSender(), now=Clock(NOW))
    cutoff = flow._log_retention_cutoff()
    expected = (NOW - timedelta(days=LOG_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    assert cutoff == expected


@pytest.mark.asyncio
async def test_admin_quick_entry_logs_allowed_and_denied(tmp_path, monkeypatch, caplog):
    from core import guardrails

    store = Store(str(tmp_path / "admin_log_quick.db"))
    store.init_schema()
    ensure_default_users(store)
    flow = ChatFlow(store, llm=None, sender=FakeSender(), now=Clock(NOW))

    decision = RouteDecision(
        route="admin_quick", admin_tier="quick", passphrase_detected=True,
        directive="додай життя", sender=SON, reason="test",
    )
    msg = ChatMessage(1, SON, "user", "Obey me! додай життя", "2026-10-20 12:00:00")

    with caplog.at_level(logging.INFO, logger="app.flow"):
        await flow._handle_admin_quick(CHAT, decision, [msg], [], trigger="push")
    assert any(
        "mode=admin_quick" in r.message and "trigger=push" in r.message and "allowed=True" in r.message
        for r in caplog.records
    )

    caplog.clear()
    monkeypatch.setattr(guardrails, "ADMIN_TELEGRAM_IDS", [999999])
    with caplog.at_level(logging.INFO, logger="app.flow"):
        await flow._handle_admin_quick(CHAT, decision, [msg], [], trigger="push")
    assert any("allowed=False" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_admin_dev_entry_logs_trigger_and_user(tmp_path, caplog):
    store = Store(str(tmp_path / "admin_log_dev.db"))
    store.init_schema()
    ensure_default_users(store)
    flow = ChatFlow(store, llm=None, sender=FakeSender(), now=Clock(NOW))

    decision = RouteDecision(
        route="admin_dev", admin_tier="dev", passphrase_detected=True,
        directive="зміни стиль", sender=SON, reason="test",
    )
    msg = ChatMessage(1, SON, "user", "Obey me! зміни стиль", "2026-10-20 12:00:00")

    with caplog.at_level(logging.INFO, logger="app.flow"):
        await flow._handle_admin_dev(CHAT, decision, [msg], [], trigger="startup")
    assert any(
        "mode=admin_dev" in r.message and "trigger=startup" in r.message and "user_name=" + SON in r.message
        for r in caplog.records
    )
