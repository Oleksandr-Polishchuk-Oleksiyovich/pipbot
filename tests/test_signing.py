"""
Unit tests for core/signing.py:
HMAC signing, timestamp freshness window, canonicalization, tampering, replay resistance.
"""
import time
import pytest
from core.signing import (
    canonical_json,
    sign_message,
    sign_payload,
    verify_message,
    verify_payload,
)


def test_sign_and_verify_message():
    secret = "super_secret_test_key"
    ts = int(time.time())
    body = "deploy:commit123"

    sig = sign_message(body, ts, secret)
    assert isinstance(sig, str) and len(sig) == 64

    # Happy path
    ok, err = verify_message(body, ts, sig, secret, max_age_seconds=300)
    assert ok is True
    assert err is None

    # Bad signature
    ok, err = verify_message(body, ts, "deadbeef" * 8, secret, max_age_seconds=300)
    assert ok is False
    assert err == "invalid_signature"

    # Bad secret
    ok, err = verify_message(body, ts, sig, "different_secret", max_age_seconds=300)
    assert ok is False
    assert err == "invalid_signature"

    # Tampered body
    ok, err = verify_message("deploy:commit999", ts, sig, secret, max_age_seconds=300)
    assert ok is False
    assert err == "invalid_signature"

    # Expired timestamp (older than 300s)
    old_ts = ts - 600
    old_sig = sign_message(body, old_ts, secret)
    ok, err = verify_message(body, old_ts, old_sig, secret, max_age_seconds=300)
    assert ok is False
    assert err == "expired_ts"

    # Future timestamp (> 300s into future)
    future_ts = ts + 600
    future_sig = sign_message(body, future_ts, secret)
    ok, err = verify_message(body, future_ts, future_sig, secret, max_age_seconds=300)
    assert ok is False
    assert err == "expired_ts"


def test_canonical_json_ordering_and_signature_exclusion():
    payload1 = {"b": 2, "a": 1, "signature": "should_be_ignored"}
    payload2 = {"a": 1, "b": 2}
    assert canonical_json(payload1) == canonical_json(payload2)
    assert canonical_json(payload1) == '{"a":1,"b":2}'


def test_sign_and_verify_payload():
    secret = "my_gateway_hmac_secret"
    ts = int(time.time())
    payload = {
        "contract_version": "1.0",
        "request_id": "req-123",
        "sha": "a1b2c3d4",
        "chat_id": -100123456,
        "reply_text": "Deploying now",
        "ts": ts,
    }

    sig = sign_payload(payload, secret)
    payload["signature"] = sig

    ok, err = verify_payload(payload, secret, max_age_seconds=300)
    assert ok is True
    assert err is None

    # Missing signature
    p_no_sig = dict(payload)
    del p_no_sig["signature"]
    ok, err = verify_payload(p_no_sig, secret)
    assert ok is False
    assert err == "missing_signature"

    # Missing ts
    p_no_ts = dict(payload)
    del p_no_ts["ts"]
    ok, err = verify_payload(p_no_ts, secret)
    assert ok is False
    assert err == "missing_ts"

    # Tampered sha
    p_tampered = dict(payload)
    p_tampered["sha"] = "modified_sha"
    ok, err = verify_payload(p_tampered, secret)
    assert ok is False
    assert err == "invalid_signature"
