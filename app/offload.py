"""
app/offload.py: Client for offloading heavy tasks (admin_dev, video) to Grok Bot box via webhook routine (T-40).
Implements Contract 8.1 with HMAC-SHA256 signing, idempotency, and status tracking in offload_requests.
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional, Union

import aiohttp

from core.config import (
    ROUTINE_SENDER_KEY,
    ROUTINE_SHARED_SECRET,
    ROUTINE_TIMEOUT_SECONDS,
    ROUTINE_WEBHOOK_URL,
    TIMEZONE,
)
from core.signing import canonical_json, sign_payload, verify_payload
from database.store import OffloadRequest, Store

logger = logging.getLogger(__name__)


def build_offload_payload(
    request_id: str,
    request_type: str,
    *,
    chat_id: Optional[int] = None,
    message_id: Optional[int] = None,
    reply_to_message_id: Optional[int] = None,
    user: Optional[Dict[str, Any]] = None,
    payload_data: Optional[Dict[str, Any]] = None,
    file_id: Optional[str] = None,
    file_unique_id: Optional[str] = None,
    reply_mode: str = "callback",
    created_at: Optional[str] = None,
    ts: Optional[int] = None,
    secret: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Будує словник payload згідно з Контрактом 8.1 (ARCHITECTURE).
    Якщо передано secret — підписує HMAC-SHA256.
    """
    now_dt = datetime.now(TIMEZONE)
    created_at_str = created_at or now_dt.isoformat()
    ts_val = int(now_dt.timestamp()) if ts is None else int(ts)

    data: Dict[str, Any] = {
        "contract_version": "1.0",
        "request_id": str(request_id),
        "type": str(request_type),
        "created_at": created_at_str,
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_to_message_id": reply_to_message_id,
        "user": user or {},
        "payload": payload_data or {},
        "file_id": file_id,
        "file_unique_id": file_unique_id,
        "reply_mode": reply_mode,
        "ts": ts_val,
    }

    if secret:
        data["signature"] = sign_payload(data, secret)
    else:
        data["signature"] = ""

    return data


class RoutineOffloadClient:
    """
    Клієнт для відправки задач на Grok Bot box через routine webhook.
    Відповідає за:
    1. Формування контракту 8.1.
    2. HMAC-SHA256 підпис payload.
    3. Запис у таблицю offload_requests з expires_at.
    4. Ідемпотентність за request_id (повторний запит — no-op).
    5. HTTP POST на webhook URL з sender key.
    """

    def __init__(
        self,
        store: Store,
        webhook_url: Optional[str] = None,
        sender_key: Optional[str] = None,
        shared_secret: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
    ):
        self.store = store
        self.webhook_url = webhook_url if webhook_url is not None else ROUTINE_WEBHOOK_URL
        self.sender_key = sender_key if sender_key is not None else ROUTINE_SENDER_KEY
        self.shared_secret = shared_secret if shared_secret is not None else ROUTINE_SHARED_SECRET
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else ROUTINE_TIMEOUT_SECONDS
        self.now = now_fn or (lambda: datetime.now(TIMEZONE))

    async def offload(
        self,
        request_type: str,
        *,
        request_id: Optional[str] = None,
        chat_id: Optional[int] = None,
        message_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
        user: Optional[Dict[str, Any]] = None,
        payload_data: Optional[Dict[str, Any]] = None,
        file_id: Optional[str] = None,
        file_unique_id: Optional[str] = None,
        reply_mode: str = "callback",
        expires_in_seconds: Optional[int] = None,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> Dict[str, Any]:
        """
        Створює запит на офлоад та відправляє його через webhook.
        Повертає словник з результатами та створеним OffloadRequest.
        """
        req_id = str(request_id) if request_id else str(uuid.uuid4())

        # Ідемпотентність: якщо request_id вже існує в базі
        existing = self.store.get_offload_request(req_id)
        if existing is not None:
            logger.info("Offload request %s already exists with status=%s; skipping duplicate", req_id, existing.status)
            return {
                "request_id": req_id,
                "status": existing.status,
                "message": "already_exists",
                "offload_request": existing,
                "is_duplicate": True,
            }

        now_dt = self.now()
        timeout_s = expires_in_seconds if expires_in_seconds is not None else self.timeout_seconds
        expires_at_dt = now_dt + timedelta(seconds=timeout_s)

        # 1. Формуємо payload за контрактом 8.1
        payload = build_offload_payload(
            request_id=req_id,
            request_type=request_type,
            chat_id=chat_id,
            message_id=message_id,
            reply_to_message_id=reply_to_message_id,
            user=user,
            payload_data=payload_data,
            file_id=file_id,
            file_unique_id=file_unique_id,
            reply_mode=reply_mode,
            created_at=now_dt.isoformat(),
            ts=int(now_dt.timestamp()),
            secret=self.shared_secret if self.shared_secret else None,
        )

        author_slug = user.get("slug") if user else None

        # 2. Зберігаємо запис у базі offload_requests
        db_request = self.store.create_offload_request(
            request_id=req_id,
            type=request_type,
            chat_id=chat_id,
            message_id=message_id,
            reply_to_message_id=reply_to_message_id,
            author_slug=author_slug,
            payload=payload_data,
            status="sent",
            expires_at=expires_at_dt,
        )

        # 3. Якщо вказано webhook_url — відправляємо HTTP POST
        http_status: Optional[int] = None
        if self.webhook_url:
            headers = {"Content-Type": "application/json"}
            if self.sender_key:
                headers["X-Sender-Key"] = self.sender_key
                headers["Authorization"] = f"Bearer {self.sender_key}"

            try:
                close_session = False
                client_session = session
                if client_session is None:
                    client_session = aiohttp.ClientSession()
                    close_session = True

                try:
                    async with client_session.post(
                        self.webhook_url,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        http_status = resp.status
                        if resp.status in (200, 201, 202):
                            logger.info("Offload request %s acked by webhook (status %d)", req_id, http_status)
                            self.store.update_offload_request(req_id, status="acked")
                            db_request = self.store.get_offload_request(req_id) or db_request
                        else:
                            logger.warning(
                                "Offload request %s webhook returned non-success code %d",
                                req_id,
                                resp.status,
                            )
                finally:
                    if close_session and client_session is not None:
                        await client_session.close()
            except Exception as e:
                logger.warning("Failed to POST offload request %s to %s: %s", req_id, self.webhook_url, e)

        return {
            "request_id": req_id,
            "status": db_request.status,
            "payload": payload,
            "offload_request": db_request,
            "http_status": http_status,
            "is_duplicate": False,
        }
