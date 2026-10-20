"""
Offline unit tests for sportnerd-gateway (T-22).
Tests /healthz, POST /deploy authentication (bad HMAC, replay, expired ts),
idempotency, pending_replies storage, and GET /deploy/<request_id>.
"""
import asyncio
import json
import os
import time
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import test_utils

from core.signing import sign_payload
from database.store import Store
from gateway.app import create_app


TEST_SECRET = "test_gateway_hmac_secret_xyz_123"


@pytest.fixture
def test_setup(tmp_path):
    db_file = str(tmp_path / "gateway_test.db")
    store = Store(db_file)
    store.init_schema()

    status_dir = str(tmp_path / "deploy_status")
    os.makedirs(status_dir, exist_ok=True)

    # Фіктивний deploy скрипт
    mock_script = str(tmp_path / "mock_deploy.sh")
    with open(mock_script, "w") as f:
        f.write("#!/bin/bash\nexit 0\n")
    os.chmod(mock_script, 0o755)

    app = create_app(
        store=store,
        secret=TEST_SECRET,
        deploy_script=mock_script,
        status_dir=status_dir,
    )
    return {
        "app": app,
        "store": store,
        "status_dir": status_dir,
        "mock_script": mock_script,
    }


@pytest.mark.asyncio
async def test_gateway_healthz(test_setup):
    """GET /healthz повертає 200 {"status": "ok"}."""
    app = test_setup["app"]
    client = test_utils.TestClient(test_utils.TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/healthz")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_gateway_deploy_bad_hmac(test_setup):
    """POST /deploy з неправильним підписом повертає 401."""
    app = test_setup["app"]
    client = test_utils.TestClient(test_utils.TestServer(app))
    await client.start_server()
    try:
        payload = {
            "contract_version": "1.0",
            "request_id": "req-bad-hmac",
            "sha": "abc12345",
            "chat_id": -1001,
            "ts": int(time.time()),
            "signature": "bad_signature_value_12345",
        }
        resp = await client.post("/deploy", json=payload)
        assert resp.status == 401
        data = await resp.json()
        assert data["error"] == "unauthorized"
        assert data["detail"] == "invalid_signature"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_gateway_deploy_expired_ts(test_setup):
    """POST /deploy з протермінованим timestamp (> 5 хв) повертає 401."""
    app = test_setup["app"]
    client = test_utils.TestClient(test_utils.TestServer(app))
    await client.start_server()
    try:
        expired_ts = int(time.time()) - 600
        payload = {
            "contract_version": "1.0",
            "request_id": "req-expired",
            "sha": "abc12345",
            "chat_id": -1001,
            "ts": expired_ts,
        }
        sig = sign_payload(payload, TEST_SECRET)
        payload["signature"] = sig

        resp = await client.post("/deploy", json=payload)
        assert resp.status == 401
        data = await resp.json()
        assert data["error"] == "unauthorized"
        assert data["detail"] == "expired_ts"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_gateway_deploy_happy_path(test_setup):
    """
    POST /deploy happy path:
    - повертає 202
    - записує pending_reply у БД
    - створює статус-файл
    - GET /deploy/<request_id> повертає статус
    """
    app = test_setup["app"]
    store: Store = test_setup["store"]
    client = test_utils.TestClient(test_utils.TestServer(app))
    await client.start_server()
    try:
        req_id = "req-deploy-001"
        payload = {
            "contract_version": "1.0",
            "request_id": req_id,
            "sha": "deadbeef1234",
            "chat_id": -100123,
            "reply_to_message_id": 456,
            "reply_text": "Деплой успішно виконано!",
            "ts": int(time.time()),
        }
        sig = sign_payload(payload, TEST_SECRET)
        payload["signature"] = sig

        resp = await client.post("/deploy", json=payload)
        assert resp.status == 202
        data = await resp.json()
        assert data["status"] == "running"
        assert data["request_id"] == req_id
        assert data["sha"] == "deadbeef1234"

        # Перевірка pending_replies у БД (T-13 Store)
        pending = store.list_pending_replies()
        assert len(pending) == 1
        assert pending[0].request_id == req_id
        assert pending[0].chat_id == -100123
        assert pending[0].text == "Деплой успішно виконано!"
        assert pending[0].reply_to_message_id == 456

        # Чекаємо завершення асинхронного процесу
        await asyncio.sleep(0.1)

        # Перевіряємо GET /deploy/<request_id>
        status_resp = await client.get(f"/deploy/{req_id}")
        assert status_resp.status == 200
        status_data = await status_resp.json()
        assert status_data["request_id"] == req_id
        assert status_data["sha"] == "deadbeef1234"
        assert status_data["status"] in ("ok", "running")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_gateway_deploy_idempotency(test_setup):
    """Повторний POST /deploy з тим самим request_id є no-op і повертає 202."""
    app = test_setup["app"]
    client = test_utils.TestClient(test_utils.TestServer(app))
    await client.start_server()
    try:
        req_id = "req-idempotent-999"
        payload = {
            "contract_version": "1.0",
            "request_id": req_id,
            "sha": "cafe0001",
            "chat_id": -100555,
            "reply_text": "Done",
            "ts": int(time.time()),
        }
        sig = sign_payload(payload, TEST_SECRET)
        payload["signature"] = sig

        # Перший виклик
        resp1 = await client.post("/deploy", json=payload)
        assert resp1.status == 202
        await asyncio.sleep(0.1)

        # Другий виклик з тим самим request_id
        resp2 = await client.post("/deploy", json=payload)
        assert resp2.status == 202
        data2 = await resp2.json()
        assert data2["request_id"] == req_id
        assert data2["message"] == "already_accepted"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_gateway_deploy_status_not_found(test_setup):
    """GET /deploy/<request_id> для невідомого id повертає 404."""
    app = test_setup["app"]
    client = test_utils.TestClient(test_utils.TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/deploy/unknown-id-12345")
        assert resp.status == 404
        data = await resp.json()
        assert data["error"] == "not_found"
    finally:
        await client.close()
