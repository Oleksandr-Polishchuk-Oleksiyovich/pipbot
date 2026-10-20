"""
Offline unit tests for Phase 4:
- T-40: offload_requests + request contract + HMAC signer (bot -> routine)
- T-42: Timeout/expiry job and user notifications
"""
import asyncio
import json
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.flow import ChatFlow
from app.offload import RoutineOffloadClient, build_offload_payload
from core.signing import canonical_json, sign_payload, verify_payload
from database.store import Store
from tests.flow_fixtures import Clock, FakeSender, NOW


TEST_SECRET = "offload_test_shared_secret_abc987"
TEST_SENDER_KEY = "routine_sender_key_xyz123"


@pytest.fixture
def store(tmp_path):
    db_file = str(tmp_path / "offload_test.db")
    st = Store(db_file)
    st.init_schema()
    return st


# ============================================================================
# T-40: offload payload formation, signing, store persistence, and idempotency
# ============================================================================

def test_build_offload_payload_contract_and_signing():
    """
    Контракт 8.1:
    - містить усі обов'язкові поля
    - підпис HMAC-SHA256 валідується через verify_payload
    - підробка будь-якого поля ламає підпис
    """
    req_id = "req-offload-001"
    user = {"telegram_id": 12345, "username": "alex", "slug": "lyosha"}
    payload_data = {"directive": "Зроби віджимання легшими", "recent_messages": ["Привіт", "Як справи"]}

    payload = build_offload_payload(
        request_id=req_id,
        request_type="admin",
        chat_id=-100123456,
        message_id=501,
        reply_to_message_id=500,
        user=user,
        payload_data=payload_data,
        secret=TEST_SECRET,
    )

    # 1. Перевірка полів контракту 8.1
    assert payload["contract_version"] == "1.0"
    assert payload["request_id"] == req_id
    assert payload["type"] == "admin"
    assert payload["chat_id"] == -100123456
    assert payload["message_id"] == 501
    assert payload["reply_to_message_id"] == 500
    assert payload["user"] == user
    assert payload["payload"] == payload_data
    assert isinstance(payload["ts"], int)
    assert payload["signature"]

    # 2. Валідація підпису через verify_payload
    valid, reason = verify_payload(payload, secret=TEST_SECRET)
    assert valid is True
    assert reason is None

    # 3. Підробка payload ламає перевірку
    tampered = dict(payload)
    tampered["chat_id"] = -999999
    valid_t, reason_t = verify_payload(tampered, secret=TEST_SECRET)
    assert valid_t is False
    assert reason_t == "invalid_signature"

    # 4. Протермінований timestamp
    expired = dict(payload)
    expired["ts"] = int(time.time()) - 600
    valid_e, reason_e = verify_payload(expired, secret=TEST_SECRET)
    assert valid_e is False
    assert reason_e == "expired_ts"


@pytest.mark.asyncio
async def test_offload_client_store_persistence_and_idempotency(store):
    """
    RoutineOffloadClient.offload:
    - записує новий запит у таблицю offload_requests зі статусом 'sent'
    - вираховує expires_at згідно з таймаутом
    - повторний виклик з тим самим request_id є no-op (idempotent)
    """
    client = RoutineOffloadClient(
        store=store,
        shared_secret=TEST_SECRET,
        timeout_seconds=900,
        now_fn=lambda: NOW,
    )

    req_id = "idem-req-offload-100"
    res1 = await client.offload(
        request_type="admin",
        request_id=req_id,
        chat_id=-100111,
        message_id=222,
        user={"slug": "lyosha", "telegram_id": 111},
        payload_data={"text": "Новий промпт"},
    )

    assert res1["request_id"] == req_id
    assert res1["status"] == "sent"
    assert res1["is_duplicate"] is False

    # Перевіряємо збереження в БД
    saved = store.get_offload_request(req_id)
    assert saved is not None
    assert saved.request_id == req_id
    assert saved.type == "admin"
    assert saved.chat_id == -100111
    assert saved.author_slug == "lyosha"
    assert saved.status == "sent"
    assert saved.expires_at is not None

    # Повторний виклик з тим самим request_id
    res2 = await client.offload(
        request_type="admin",
        request_id=req_id,
        chat_id=-100111,
        message_id=222,
        user={"slug": "lyosha"},
    )
    assert res2["is_duplicate"] is True
    assert res2["status"] == "sent"

    # Кількість записів у БД не змінилась
    all_requests = store.list_offload_requests()
    assert len(all_requests) == 1


@pytest.mark.asyncio
async def test_offload_client_mocked_http_success_and_ack(store):
    """
    RoutineOffloadClient з HTTP вебхуком:
    - надсилає POST з заголовками (X-Sender-Key, Authorization)
    - при відповіді 200/202 оновлює статус запиту на 'acked'
    """
    received_requests = []

    async def mock_routine_handler(request: web.Request) -> web.Response:
        data = await request.json()
        headers = dict(request.headers)
        received_requests.append({"data": data, "headers": headers})
        return web.json_response({"ok": True, "task_id": "routine-task-42"}, status=200)

    mock_app = web.Application()
    mock_app.router.add_post("/routine/webhook", mock_routine_handler)

    test_client = TestClient(TestServer(mock_app))
    await test_client.start_server()

    try:
        webhook_url = str(test_client.make_url("/routine/webhook"))
        client = RoutineOffloadClient(
            store=store,
            webhook_url=webhook_url,
            sender_key=TEST_SENDER_KEY,
            shared_secret=TEST_SECRET,
            now_fn=lambda: NOW,
        )

        req_id = "http-offload-req-001"
        res = await client.offload(
            request_type="admin",
            request_id=req_id,
            chat_id=-100222,
            message_id=333,
            user={"slug": "sasha", "telegram_id": 222},
            payload_data={"text": "Змінити складність на hardcore"},
        )

        assert res["request_id"] == req_id
        assert res["http_status"] == 200
        assert res["status"] == "acked"

        # Перевірка що в БД статус оновлено на 'acked'
        in_db = store.get_offload_request(req_id)
        assert in_db.status == "acked"

        # Перевірка отриманого вебхуком запиту
        assert len(received_requests) == 1
        received = received_requests[0]
        assert received["headers"].get("X-Sender-Key") == TEST_SENDER_KEY
        assert received["headers"].get("Authorization") == f"Bearer {TEST_SENDER_KEY}"
        assert received["data"]["request_id"] == req_id
        assert received["data"]["user"]["slug"] == "sasha"

        # Підпис у вебхуку валідний
        valid, reason = verify_payload(received["data"], TEST_SECRET, now=int(NOW.timestamp()))
        assert valid is True, f"Verification failed: {reason}"
    finally:
        await test_client.close()


@pytest.mark.asyncio
async def test_offload_client_http_failure_handling(store):
    """
    При мережевій помилці або 500 від вебхука:
    - запис лишається в БД зі статусом 'sent'
    - виняток ловиться без аварійного завершення
    """
    async def mock_error_handler(request: web.Request) -> web.Response:
        return web.json_response({"error": "box_overloaded"}, status=503)

    mock_app = web.Application()
    mock_app.router.add_post("/routine/webhook", mock_error_handler)

    test_client = TestClient(TestServer(mock_app))
    await test_client.start_server()

    try:
        webhook_url = str(test_client.make_url("/routine/webhook"))
        client = RoutineOffloadClient(
            store=store,
            webhook_url=webhook_url,
            sender_key=TEST_SENDER_KEY,
            shared_secret=TEST_SECRET,
            now_fn=lambda: NOW,
        )

        req_id = "http-fail-req-002"
        res = await client.offload(
            request_type="video",
            request_id=req_id,
            chat_id=-100333,
            message_id=444,
            payload_data={"video_id": "file-123"},
        )

        assert res["request_id"] == req_id
        assert res["http_status"] == 503
        assert res["status"] == "sent"

        # Запис у БД лишився зі статусом 'sent'
        in_db = store.get_offload_request(req_id)
        assert in_db.status == "sent"
    finally:
        await test_client.close()


# ============================================================================
# T-42: Timeout/expiry check job and notifications
# ============================================================================

@pytest.mark.asyncio
async def test_flow_check_expired_offload_requests(store):
    """
    T-42 Expiry check:
    - знаходить прострочені запити зі статусом 'sent'/'acked'
    - переводить статус у 'expired'
    - надсилає повідомлення користувачу в чат (persona) реплаєм на повідомлення
    - не чіпає ще дійсні запити
    """
    clock = Clock(NOW)
    sender = FakeSender()
    flow = ChatFlow(store, None, sender, now=clock)

    now_dt = clock()
    past_dt = now_dt - timedelta(minutes=20)
    future_dt = now_dt + timedelta(minutes=15)

    # 1. Прострочений admin-запит
    req_admin_expired = "req-admin-exp-01"
    store.create_offload_request(
        request_id=req_admin_expired,
        type="admin",
        chat_id=-100555,
        message_id=101,
        reply_to_message_id=100,
        status="sent",
        expires_at=past_dt,
    )

    # 2. Прострочений video-запит
    req_video_expired = "req-video-exp-02"
    store.create_offload_request(
        request_id=req_video_expired,
        type="video",
        chat_id=-100555,
        message_id=202,
        reply_to_message_id=201,
        status="acked",
        expires_at=past_dt,
    )

    # 3. Активний запит (не прострочений)
    req_active = "req-active-03"
    store.create_offload_request(
        request_id=req_active,
        type="admin",
        chat_id=-100555,
        message_id=303,
        status="sent",
        expires_at=future_dt,
    )

    # 4. Виклик periodic check
    expired_ids = await flow.check_expired_offload_requests(default_chat_id=-100555)

    assert set(expired_ids) == {req_admin_expired, req_video_expired}

    # Перевірка статусів у БД
    assert store.get_offload_request(req_admin_expired).status == "expired"
    assert store.get_offload_request(req_video_expired).status == "expired"
    assert store.get_offload_request(req_active).status == "sent"

    # Перевірка надісланих повідомлень
    assert len(sender.texts) == 2

    # Повідомлення про прострочений admin-запит
    admin_msg = next(txt for txt in sender.texts if req_admin_expired in txt)
    assert "прострочено" in admin_msg

    # Повідомлення про прострочене відео
    video_msg = next(txt for txt in sender.texts if "відео" in txt.lower())
    assert "вичерпано" in video_msg.lower()

    # Перевірка replies (reply_to_message_id)
    assert any(reply_id == 100 for reply_id, _ in sender.replies)
    assert any(reply_id == 201 for reply_id, _ in sender.replies)


@pytest.mark.asyncio
async def test_flow_timer_trigger_offload_expiry(store):
    """Перевірка що flow.process(..., trigger='timer', job='offload_expiry') викликає check_expired_offload_requests."""
    clock = Clock(NOW)
    sender = FakeSender()
    flow = ChatFlow(store, None, sender, now=clock)

    past_dt = clock() - timedelta(minutes=5)
    req_id = "req-timer-exp-01"
    store.create_offload_request(
        request_id=req_id,
        type="admin",
        chat_id=-100777,
        message_id=55,
        status="sent",
        expires_at=past_dt,
    )

    await flow.process(-100777, trigger="timer", job="offload_expiry")

    assert store.get_offload_request(req_id).status == "expired"
    assert len(sender.texts) == 1
    assert req_id in sender.texts[0]
