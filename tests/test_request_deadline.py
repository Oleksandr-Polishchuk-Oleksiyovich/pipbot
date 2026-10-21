import time
import urllib.request

import pytest

from cv_worker import box_worker as bw


class _Resp:
    status = 204
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return b""


def test_deadline_fires_when_request_stalls(monkeypatch):
    monkeypatch.setattr(bw.urllib.request, "urlopen", lambda req, timeout=0: time.sleep(5))
    req = urllib.request.Request("http://gw/cv/claim", method="POST")
    t0 = time.monotonic()
    with pytest.raises(bw.RequestDeadlineExceeded):
        bw.urlopen_with_deadline(req, timeout=0.1, deadline=0.3)
    assert time.monotonic() - t0 < 2


def test_fast_request_returns_and_errors_propagate(monkeypatch):
    monkeypatch.setattr(bw.urllib.request, "urlopen", lambda req, timeout=0: _Resp())
    req = urllib.request.Request("http://gw/cv/claim", method="POST")
    assert bw.urlopen_with_deadline(req, timeout=1) == (204, b"")

    def boom(req, timeout=0):
        raise OSError("conn refused")
    monkeypatch.setattr(bw.urllib.request, "urlopen", boom)
    with pytest.raises(OSError):
        bw.urlopen_with_deadline(req, timeout=1)


def test_claim_deadline_is_long_poll_plus_slack():
    assert bw.CLAIM_HTTP_TIMEOUT_S + bw.DEADLINE_SLACK_S <= 60
