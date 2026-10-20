"""
tests/test_phase5_admin_dev.py: Offline unit tests for Phase 5 tasks:
- T-50: Router admin_dev class + instant ack + offload record
- T-51: Box skill admin_dev.md structure, pipeline 1-8 and paths
- T-52: admin_audit + guardrails (allowlist, forbidden zones, /approve, rate limit, /rollback)
- T-53: Reconcile prompt_editor -> git-migrations as source of truth (offload vs fallback)
"""
import asyncio
import os
import sqlite3
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agents.admin import AdminAgent
from agents.prompt_editor import COACH_DYNAMIC_KEY, CoachPromptEditor, RuleChange
from agents.router import RouteDecision, RouterAgent, has_passphrase
from app.flow import ChatFlow
from app.offload import RoutineOffloadClient
from core import challenge
from core.config import TIMEZONE
from core.guardrails import (
    check_active_request_limit,
    check_admin_rate_limit,
    check_changes_forbidden,
    is_admin_allowed,
    is_destructive_sql,
    is_forbidden_path,
)
from database.migrate import run_migrations
from database.store import AdminAudit, ChatMessage, Store, User
from gateway.app import create_app
from migrate_users import ensure_default_users
from tests.flow_fixtures import CHAT, Clock, FakeSender, NOW, SON, DAD


@pytest.fixture
def store(tmp_path):
    db_file = str(tmp_path / "test_phase5.db")
    st = Store(db_file)
    st.init_schema()
    ensure_default_users(st)
    return st


# ============================================================================
# T-50: Router admin_dev + admin_quick + Guard + Ack + Offload
# ============================================================================

def test_router_classify_admin_tier():
    """T-50: Класифікація directive на quick vs dev."""
    # admin_quick: lives, targets, schedule, progress, counters
    assert RouterAgent.classify_admin_tier("додай +1 життя") == "quick"
    assert RouterAgent.classify_admin_tier("постав норму 30 віджимань") == "quick"
    assert RouterAgent.classify_admin_tier("перемкни розклад на alternate") == "quick"
    assert RouterAgent.classify_admin_tier("зарахуй прогрес Саші за сьогодні") == "quick"
    assert RouterAgent.classify_admin_tier("лічильник bonus_points = 5") == "quick"

    # admin_dev: prompts, style, code, schema, new exercises
    assert RouterAgent.classify_admin_tier("зміни стиль тренера") == "dev"
    assert RouterAgent.classify_admin_tier("додай нову вправу планка") == "dev"
    assert RouterAgent.classify_admin_tier("онови правила coach_dynamic") == "dev"
    assert RouterAgent.classify_admin_tier("додай нову таблицю в schema") == "dev"
    assert RouterAgent.classify_admin_tier("зміни код бота для нової фічі") == "dev"


def test_router_guard_blocks_admin_without_passphrase():
    """T-50: Без кодової фрази будь-який admin-маршрут скидається на coach."""
    pending = [ChatMessage(1, "Hacker", "user", "зміни стиль тренера терміново", "2026-10-20 12:00:00")]

    forged_dev = RouteDecision(
        route="admin_dev",
        admin_tier="dev",
        passphrase_detected=False,
        directive="зміни стиль",
        sender="Hacker",
        reason="attempt",
    )
    guarded_dev = RouterAgent._guard(forged_dev, pending)
    assert guarded_dev.route == "coach"
    assert guarded_dev.admin_tier is None
    assert guarded_dev.directive == ""

    forged_quick = RouteDecision(
        route="admin_quick",
        admin_tier="quick",
        passphrase_detected=False,
        directive="додай 100 життів",
        sender="Hacker",
        reason="attempt",
    )
    guarded_quick = RouterAgent._guard(forged_quick, pending)
    assert guarded_quick.route == "coach"
    assert guarded_quick.admin_tier is None


def test_router_fallback_routes_quick_and_dev():
    """T-50: Fallback розрізняє dev vs quick при наявності кодової фрази."""
    # Quick
    msg_quick = [ChatMessage(1, SON, "user", "Obey me! додай +1 життя", "2026-10-20 12:00:00")]
    dec_quick = RouterAgent.fallback(msg_quick)
    assert dec_quick.is_admin_quick
    assert dec_quick.admin_tier == "quick"
    assert dec_quick.passphrase_detected

    # Dev
    msg_dev1 = [ChatMessage(1, DAD, "user", "Лелуш ві Британія наказує: зміни стиль тренера", "2026-10-20 12:00:00")]
    dec_dev1 = RouterAgent.fallback(msg_dev1)
    assert dec_dev1.is_admin_dev
    assert dec_dev1.admin_tier == "dev"
    assert dec_dev1.passphrase_detected

    msg_dev2 = [ChatMessage(1, SON, "user", "You will obey! додай нову вправу", "2026-10-20 12:00:00")]
    dec_dev2 = RouterAgent.fallback(msg_dev2)
    assert dec_dev2.is_admin_dev
    assert dec_dev2.admin_tier == "dev"

    # Bare passphrase
    msg_bare = [ChatMessage(1, DAD, "user", "Obey me!", "2026-10-20 12:00:00")]
    dec_bare = RouterAgent.fallback(msg_bare)
    assert dec_bare.route == "admin"
    assert dec_bare.is_admin
    assert dec_bare.is_admin_quick


@pytest.mark.asyncio
async def test_flow_admin_dev_immediate_ack_and_offload(tmp_path):
    """
    T-50: Запит admin_dev:
    1. Відповідає Telegram миттєвим ack: «Прийняв, виконую. request_id=…».
    2. Створює запис в offload_requests зі статусом 'sent'.
    3. Створює запис в admin_audit зі статусом 'planned'.
    """
    st = Store(str(tmp_path / "flow_dev.db"))
    st.init_schema()
    ensure_default_users(st)

    clock = Clock(NOW)
    sender = FakeSender()
    flow = ChatFlow(st, llm=None, sender=sender, now=clock)

    # Автор з кодовою фразою та dev-наказом
    flow.record_user_message(CHAT, SON, "Obey me! зміни стиль тренера на суворий", created_at=clock())
    await flow.process(CHAT, trigger="push")

    # 1. Перевіряємо відповідь в Telegram
    assert len(sender.texts) > 0
    ack_reply = sender.texts[-1]
    assert "Прийняв, виконую. request_id=" in ack_reply

    # 2. Перевіряємо offload_requests
    offload_reqs = st.list_offload_requests()
    assert len(offload_reqs) == 1
    req = offload_reqs[0]
    assert req.type == "admin"
    assert req.status == "sent"
    assert req.request_id in ack_reply

    # 3. Перевіряємо admin_audit (T-52)
    audits = st.list_admin_audits()
    assert len(audits) == 1
    assert audits[0].request_id == req.request_id
    assert audits[0].status == "planned"


@pytest.mark.asyncio
async def test_flow_admin_quick_executes_in_bot(tmp_path):
    """
    T-50: Запит admin_quick виконується in-bot миттєво, без offload на box.
    """
    st = Store(str(tmp_path / "flow_quick.db"))
    st.init_schema()
    ensure_default_users(st)

    clock = Clock(NOW)
    sender = FakeSender()
    flow = ChatFlow(st, llm=None, sender=sender, now=clock)

    # Автор з кодовою фразою на quick команду (додати життя Льоші)
    initial_lives = challenge.get_lives(st)["lyosha"]
    flow.record_user_message(CHAT, SON, "Obey me! додай +1 життя", created_at=clock())
    await flow.process(CHAT, trigger="push")

    # In-bot: offload_requests НЕ створюється для admin_quick
    assert len(st.list_offload_requests()) == 0


# ============================================================================
# T-51: Box skill admin_dev.md
# ============================================================================

def test_skill_admin_dev_file_and_contents():
    """
    T-51: skills/admin_dev.md існує, описує пайплайн 1–8, контракти 8.1/8.2/8.4,
    заборонені зони, вимогу green pytest та посилається на реальні шляхи репозиторію.
    """
    skill_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "admin_dev.md")
    assert os.path.isfile(skill_path), f"File {skill_path} does not exist"

    with open(skill_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Пайплайн 1–8
    for step_num in range(1, 9):
        assert f"Крок {step_num}." in content

    # Контракти
    assert "8.1" in content
    assert "8.2" in content
    assert "8.4" in content
    assert "POST /deploy" in content
    assert "POST /callback" in content

    # Заборонені зони та guardrails
    assert ".env" in content
    assert "deploy/*" in content
    assert "/approve" in content
    assert "/rollback" in content

    # Вимога green pytest
    assert ".venv/bin/python -m pytest -q --ignore=tests/integration" in content

    # Посилання на реальні шляхи
    for real_path in ["agents/", "app/", "core/", "database/", "migrations/", "gateway/", "deploy/"]:
        assert real_path in content


# ============================================================================
# T-52: admin_audit + guardrails (allowlist, forbidden zones, /approve, rate limit)
# ============================================================================

def test_admin_audit_store_crud(store):
    """T-52: Таблиця admin_audit і методи CRUD у Store."""
    req_id = f"test-audit-{uuid.uuid4().hex[:6]}"
    audit = store.create_admin_audit(
        request_id=req_id,
        author_slug="sasha",
        author_telegram_id=123456,
        request="зміни стиль тренера",
        plan="update prompt",
        status="planned",
    )
    assert audit.request_id == req_id
    assert audit.author_slug == "sasha"
    assert audit.status == "planned"

    fetched = store.get_admin_audit(req_id)
    assert fetched is not None
    assert fetched.request_id == req_id

    # Update status and plan
    store.update_admin_audit(req_id, status="waiting_approval", plan="touches .env")
    updated = store.get_admin_audit(req_id)
    assert updated.status == "waiting_approval"
    assert updated.plan == "touches .env"

    # List audits
    all_audits = store.list_admin_audits()
    assert any(a.request_id == req_id for a in all_audits)


def test_forbidden_zones_detection():
    """T-52: Детекція файлів у заборонених зонах."""
    forbidden = [
        ".env",
        ".env.example",
        "/workspace/pipbot/.env",
        "deploy/deploy.sh",
        "deploy/README.md",
        "systemd/sportnerdbot.service",
        ".github/workflows/deploy.yml",
        "migrations/destructive/0099_drop_all.sql",
        "my_secret_token.txt",
    ]
    for p in forbidden:
        assert is_forbidden_path(p), f"Path '{p}' should be forbidden"

    allowed = [
        "agents/coach.py",
        "app/flow.py",
        "core/challenge.py",
        "database/store.py",
        "migrations/0008_new_exercise.sql",
        "tests/test_flow_offline.py",
    ]
    for p in allowed:
        assert not is_forbidden_path(p), f"Path '{p}' should be allowed"


def test_destructive_sql_detection():
    """T-52: Детекція деструктивних SQL операцій."""
    destructive = [
        "DROP TABLE users;",
        "DROP COLUMN lives FROM users;",
        "DELETE FROM user_progress WHERE date < '2026-09-01';",
        "TRUNCATE TABLE progress;",
    ]
    for sql in destructive:
        assert is_destructive_sql(sql), f"SQL should be destructive: {sql}"

    safe = [
        "CREATE TABLE IF NOT EXISTS new_table (id INT);",
        "INSERT INTO user_progress VALUES ('2026-10-20', 'lyosha', 1);",
        "SELECT * FROM users;",
    ]
    for sql in safe:
        assert not is_destructive_sql(sql), f"SQL should be safe: {sql}"


def test_check_changes_forbidden_helper():
    """T-52: check_changes_forbidden повертає статус і перелік причин."""
    is_f, reasons = check_changes_forbidden(["agents/coach.py", "deploy/deploy.sh"])
    assert is_f
    assert any("deploy/deploy.sh" in r for r in reasons)

    is_safe, reasons_safe = check_changes_forbidden(["agents/coach.py", "database/store.py"])
    assert not is_safe
    assert len(reasons_safe) == 0


def test_admin_allowlist():
    """T-52: Перевірка allowlist telegram_id."""
    allowlist = [111, 222]
    assert is_admin_allowed(111, allowlist=allowlist)
    assert not is_admin_allowed(333, allowlist=allowlist)
    assert not is_admin_allowed(None, allowlist=allowlist)

    # Порожній allowlist дозволяє доступ за кодовою фразою
    assert is_admin_allowed(333, allowlist=[])


def test_admin_rate_limit(store):
    """T-52: Перевірка ліміту admin_dev запитів на годину (≤ N/год)."""
    now = datetime.now()
    author = "rate_limited_admin"

    # Додаємо 3 аудити
    for i in range(3):
        store.create_admin_audit(
            request_id=f"rate-{i}",
            author_slug=author,
            request="dev task",
            status="planned",
        )

    # При ліміті 3: дозволено якщо < 3, отже четвертий блокується
    allowed, msg = check_admin_rate_limit(store, author_slug=author, limit_per_hour=3, now=now)
    assert not allowed
    assert "перевищено" in msg

    # При ліміті 5: дозволено
    allowed_5, _ = check_admin_rate_limit(store, author_slug=author, limit_per_hour=5, now=now)
    assert allowed_5


def test_active_request_limit(store):
    """T-52: Один активний запит одночасно."""
    author = "active_admin"
    assert check_active_request_limit(store, author_slug=author)[0] is True

    # Створюємо активний аудит
    store.create_admin_audit(
        request_id="active-001",
        author_slug=author,
        request="dev task",
        status="planned",
    )
    allowed, active_id = check_active_request_limit(store, author_slug=author)
    assert not allowed
    assert active_id == "active-001"

    # Завершуємо запит
    store.update_admin_audit("active-001", status="deployed")
    assert check_active_request_limit(store, author_slug=author)[0] is True


@pytest.mark.asyncio
async def test_command_approve_flow(tmp_path):
    """T-52: Команда /approve <request_id> схвалює запит на деплой."""
    st = Store(str(tmp_path / "approve.db"))
    st.init_schema()
    ensure_default_users(st)

    clock = Clock(NOW)
    sender = FakeSender()
    flow = ChatFlow(st, llm=None, sender=sender, now=clock)

    # Створюємо запит, що очікує підтвердження
    req_id = "req-to-approve-42"
    st.create_admin_audit(
        request_id=req_id,
        author_slug="lyosha",
        request="touch deploy",
        status="waiting_approval",
    )

    # Адмін надсилає /approve req-to-approve-42
    flow.record_user_message(CHAT, SON, f"/approve {req_id}", created_at=clock())
    await flow.process(CHAT, trigger="push")

    # Перевіряємо оновлення статусу
    audit = st.get_admin_audit(req_id)
    assert audit.status == "approved"
    assert any("схвалено" in t for t in sender.texts)


@pytest.mark.asyncio
async def test_command_rollback_flow(tmp_path):
    """T-52: Команда /rollback фіксується в audit і викликає відкат."""
    st = Store(str(tmp_path / "rollback.db"))
    st.init_schema()
    ensure_default_users(st)

    clock = Clock(NOW)
    sender = FakeSender()
    flow = ChatFlow(st, llm=None, sender=sender, now=clock)

    flow.record_user_message(CHAT, SON, "/rollback", created_at=clock())
    await flow.process(CHAT, trigger="push")

    assert any("відкат" in t.lower() for t in sender.texts)
    audits = st.list_admin_audits()
    assert any(a.request == "/rollback" for a in audits)


# ============================================================================
# T-53: Reconcile prompt_editor -> git-migrations source of truth
# ============================================================================

def test_admin_agent_update_coach_prompt_offload_when_box_available(store):
    """
    T-53: При доступному Grok Box виклик update_coach_prompt створює offload_request
    та НЕ змінює промпт у БД напряму (чекає на git-міграцію).
    """
    mock_editor = MagicMock()
    # Ініціалізуємо AdminAgent із box_available=True
    admin = AdminAgent(
        llm=None,
        store=store,
        editor=mock_editor,
        box_available=True,
    )

    actions = []
    tools = admin._tools(today=NOW.date(), author="sasha", actions=actions)
    update_prompt_tool = next(t for t in tools if t.__name__ == "update_coach_prompt")

    result = update_prompt_tool(
        rule="Завжди хвалити за присідання",
        original_quote="хвали за присідання",
    )

    # 1. Створено offload-запит
    offload_reqs = store.list_offload_requests()
    assert len(offload_reqs) == 1
    assert offload_reqs[0].type == "admin"
    assert "update_coach_prompt" in str(offload_reqs[0].payload)

    # 2. editor.apply НЕ викликався напряму
    mock_editor.apply.assert_not_called()
    assert "передано на Grok Box" in result


def test_admin_agent_update_coach_prompt_fallback_when_box_unavailable(store):
    """
    T-53: При недоступному Grok Box виклик update_coach_prompt викликає локальний fallback,
    який записує в БД з updated_by='bot_fallback' та позначає для reconcile.
    """
    from agents.prompt_editor import PromptEditResult

    mock_editor = MagicMock()
    mock_editor.apply.return_value = PromptEditResult(
        changed=True,
        strategy="append",
        version=5,
        content="новий промпт",
        reason="ok",
    )

    admin = AdminAgent(
        llm=None,
        store=store,
        editor=mock_editor,
        box_available=False,  # Box недоступний!
    )

    actions = []
    tools = admin._tools(today=NOW.date(), author="sasha", actions=actions)
    update_prompt_tool = next(t for t in tools if t.__name__ == "update_coach_prompt")

    result = update_prompt_tool(
        rule="Термінове локальне правило",
        original_quote="термінове правило",
    )

    # 1. editor.apply викликано з updated_by='bot_fallback'
    mock_editor.apply.assert_called_once()
    args, kwargs = mock_editor.apply.call_args
    assert kwargs.get("updated_by") == "bot_fallback"

    # 2. Відповідь сповіщає про bot_fallback
    assert "bot_fallback" in result
    assert "reconcile" in result


def test_reconcile_prompts_detection(store):
    """
    T-53: get_unreconciled_prompts виявляє записи з updated_by='bot_fallback'.
    """
    # Зберігаємо промпт через fallback
    store.save_prompt(COACH_DYNAMIC_KEY, "Правило згенероване локально", updated_by="bot_fallback")

    unreconciled = store.get_unreconciled_prompts()
    assert len(unreconciled) == 1
    assert unreconciled[0]["key"] == COACH_DYNAMIC_KEY
    assert unreconciled[0]["updated_by"] == "bot_fallback"

    # Після збереження через git-міграцію unreconciled стає порожнім
    store.save_prompt(COACH_DYNAMIC_KEY, "Правило з git-міграції", updated_by="git_migration")
    assert len(store.get_unreconciled_prompts()) == 0


def test_prompt_migration_template_execution(tmp_path):
    """
    T-53: Перевірка шаблону міграції 0007_prompt_template.py через runner.
    """
    db_file = str(tmp_path / "prompt_mig.db")
    applied = run_migrations(db_file)
    assert any(name == "0007_prompt_template" for _, name in applied)

    st = Store(db_file)
    content = st.get_prompt("coach_dynamic")
    assert content is not None
    assert "Спеціальні правила тренера (v7)" in content

    # Перевіряємо історію
    with st._connect() as con:
        row = con.execute("SELECT updated_by FROM prompt_history WHERE key = 'coach_dynamic' ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == "git_migration"


@pytest.mark.asyncio
async def test_gateway_deploy_blocked_for_forbidden_zones_without_approve(tmp_path):
    """
    T-52 Acceptance Criteria: зміна в .env/deploy/* без /approve не деплоїться.
    1. Запит /deploy із файлами в забороненій зоні (напр. .env або deploy/deploy.sh)
       повертає 403 approval_required та статус waiting_approval в admin_audit.
    2. Після переведення запиту в статус 'approved' (команда /approve), деплой дозволено.
    """
    import time
    from aiohttp import test_utils
    from core.signing import sign_payload
    from gateway.app import create_app

    db_file = str(tmp_path / "gw_audit.db")
    st = Store(db_file)
    st.init_schema()

    status_dir = str(tmp_path / "status")
    os.makedirs(status_dir, exist_ok=True)
    mock_script = str(tmp_path / "mock.sh")
    with open(mock_script, "w") as f:
        f.write("#!/bin/bash\nexit 0\n")
    os.chmod(mock_script, 0o755)

    secret = "test_secret_123"
    app = create_app(store=st, secret=secret, deploy_script=mock_script, status_dir=status_dir)
    client = test_utils.TestClient(test_utils.TestServer(app))
    await client.start_server()

    try:
        req_id = "req-forbidden-001"
        ts = int(time.time())
        payload = {
            "contract_version": "1.0",
            "request_id": req_id,
            "sha": "sha123456",
            "chat_id": -100500,
            "ts": ts,
            "files": [".env", "deploy/deploy.sh"],
        }
        payload["signature"] = sign_payload(payload, secret)

        # 1. Спроба деплою без approve -> 403 Forbidden
        resp = await client.post("/deploy", json=payload)
        assert resp.status == 403
        data = await resp.json()
        assert data["error"] == "approval_required"
        assert data["status"] == "waiting_approval"

        # Перевіряємо запис в admin_audit
        audit = st.get_admin_audit(req_id)
        assert audit is not None
        assert audit.status == "waiting_approval"

        # 2. Адмін схвалює запит (/approve)
        st.update_admin_audit(req_id, status="approved")

        # 3. Повторний деплой після схвалення -> 202 Accepted
        payload["ts"] = int(time.time())
        payload["signature"] = sign_payload(payload, secret)
        resp_approved = await client.post("/deploy", json=payload)
        assert resp_approved.status == 202
        data_app = await resp_approved.json()
        assert data_app["status"] == "running"
    finally:
        await client.close()
