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


# --- Request-level signing (box CV worker <-> gateway /cv/*) ---------------------------
# Підпис над METHOD \n PATH \n TS \n NONCE \n sha256(body). Заголовки: X-SN-Ts, X-SN-Nonce, X-SN-Signature.
# Захист від replay: вікно ts ±max_age + одноразові nonce (NonceCache).

HDR_TS = "X-SN-Ts"
HDR_NONCE = "X-SN-Nonce"
HDR_SIG = "X-SN-Signature"


def _request_canonical(method: str, path: str, ts: int, nonce: str, body: bytes) -> str:
    return f"{method.upper()}\n{path}\n{int(ts)}\n{nonce}\n{hashlib.sha256(body or b'').hexdigest()}"


def sign_request(method: str, path: str, body: bytes, secret: str,
                 ts: Optional[int] = None, nonce: Optional[str] = None) -> Dict[str, str]:
    """Повертає заголовки підпису для HTTP-запиту."""
    import secrets as _secrets
    if not secret:
        raise ValueError("HMAC secret cannot be empty")
    ts = int(time.time()) if ts is None else int(ts)
    nonce = nonce or _secrets.token_hex(16)
    sig = hmac.new(secret.encode("utf-8"), _request_canonical(method, path, ts, nonce, body).encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return {HDR_TS: str(ts), HDR_NONCE: nonce, HDR_SIG: sig}


class NonceCache:
    """In-memory кеш використаних nonce з TTL (достатньо для одного процесу gateway)."""

    def __init__(self, ttl_seconds: int = 2 * DEFAULT_MAX_AGE_SECONDS):
        self.ttl = ttl_seconds
        self._seen: Dict[str, float] = {}

    def check_and_add(self, nonce: str, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        if len(self._seen) > 1000:
            self._seen = {n: t for n, t in self._seen.items() if now - t < self.ttl}
        if nonce in self._seen and now - self._seen[nonce] < self.ttl:
            return False
        self._seen[nonce] = now
        return True


def verify_request(method: str, path: str, body: bytes, headers: Any, secret: str,
                   nonces: Optional[NonceCache] = None, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
                   now: Optional[float] = None) -> Tuple[bool, Optional[str]]:
    if not secret:
        return False, "missing_secret"
    ts_raw, nonce, sig = headers.get(HDR_TS), headers.get(HDR_NONCE), headers.get(HDR_SIG)
    if not sig or not nonce or ts_raw is None:
        return False, "missing_signature"
    try:
        ts = int(ts_raw)
    except (TypeError, ValueError):
        return False, "invalid_ts"
    current = time.time() if now is None else now
    if abs(current - ts) > max_age_seconds:
        return False, "expired_ts"
    expected = hmac.new(secret.encode("utf-8"), _request_canonical(method, path, ts, nonce, body).encode("utf-8"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False, "invalid_signature"
    if nonces is not None and not nonces.check_and_add(nonce, now=current):
        return False, "replayed_nonce"
    return True, None
