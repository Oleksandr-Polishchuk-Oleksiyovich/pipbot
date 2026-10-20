"""
HMAC-SHA256 signing and verification utility for SportNerdBot.
Shared between gateway (/deploy, /callback) and routine webhook offload.
Enforces timestamp freshness window (default: ±5 minutes) and timing-attack resistant verification.
"""
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional, Tuple, Union


DEFAULT_MAX_AGE_SECONDS = 300  # ±5 хвилин


def sign_message(body: str, ts: int, secret: str) -> str:
    """
    Підписує повідомлення через HMAC-SHA256 над body || ts.
    Повертає hex-рядок підпису.
    """
    if not secret:
        raise ValueError("HMAC secret cannot be empty")
    msg = f"{body}{ts}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_message(
    body: str,
    ts: int,
    signature: str,
    secret: str,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    now: Optional[float] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Перевіряє HMAC-підпис та актуальність timestamp для текстового повідомлення.
    Повертає (True, None) при успіху або (False, reason) при невідповідності.
    """
    if not secret:
        return False, "missing_secret"
    if not signature:
        return False, "missing_signature"

    current_ts = time.time() if now is None else now
    try:
        ts_int = int(ts)
    except (TypeError, ValueError):
        return False, "invalid_ts"

    if abs(current_ts - ts_int) > max_age_seconds:
        return False, "expired_ts"

    expected_sig = sign_message(body, ts_int, secret)
    if not hmac.compare_digest(expected_sig, signature):
        return False, "invalid_signature"

    return True, None


def canonical_json(data: Dict[str, Any]) -> str:
    """
    Формує детермінований канонічний JSON (відсортовані ключі, без пробілів навколо роздільників, UTF-8).
    Виключає поле 'signature' якщо воно присутнє.
    """
    filtered = {k: v for k, v in data.items() if k != "signature"}
    return json.dumps(filtered, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sign_payload(payload: Dict[str, Any], secret: str) -> str:
    """
    Канонізує словник (виключаючи поле 'signature') і генерує HMAC-SHA256 підпис над canonical_body || ts.
    Поле 'ts' має бути присутнє у payload або буде автоматично згенеровано поточний timestamp.
    """
    if not secret:
        raise ValueError("HMAC secret cannot be empty")
    if "ts" not in payload:
        payload["ts"] = int(time.time())

    body = canonical_json(payload)
    return sign_message(body, int(payload["ts"]), secret)


def verify_payload(
    payload: Dict[str, Any],
    secret: str,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    now: Optional[float] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Перевіряє підпис словника payload (з поля 'signature') та вік timestamp ('ts').
    Повертає (True, None) або (False, reason).
    """
    if not secret:
        return False, "missing_secret"
    if not isinstance(payload, dict):
        return False, "invalid_payload_format"

    signature = payload.get("signature")
    if not signature or not isinstance(signature, str):
        return False, "missing_signature"

    ts = payload.get("ts")
    if ts is None:
        return False, "missing_ts"

    try:
        ts_int = int(ts)
    except (TypeError, ValueError):
        return False, "invalid_ts"

    current_ts = time.time() if now is None else now
    if abs(current_ts - ts_int) > max_age_seconds:
        return False, "expired_ts"

    body = canonical_json(payload)
    expected_sig = sign_message(body, ts_int, secret)

    if not hmac.compare_digest(expected_sig, signature):
        return False, "invalid_signature"

    return True, None
