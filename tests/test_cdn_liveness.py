"""
Unit tests for the MKV-sentinel CDN liveness check (app._cdn_url_alive).

The check exists because /spore-stream 302s MKV playback straight at a
cached CDN link: unlike the MP4 path it never proxies the bytes, so a retired
link reaches ffmpeg as a redirect into an error page with no way back. The rule
under test is which statuses mean "retired" (re-resolve) versus "alive but busy"
(keep the URL). A 429 in the second group is the load-bearing case: TorBox hands
back the same URL, so re-resolving a throttled link doubles the request rate that
caused the throttle. spore-nfs draws the same line with errRateLimited.
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("TORBOX_API_KEY", "test")
os.environ.setdefault("MEDIA_PATH", "/tmp/mycelium-test-media")
os.environ.setdefault("SPORE_MEDIA_PATH", "/tmp/mycelium-test-spore")
os.environ.setdefault("TORBOX_BASE_URL", "https://api.torbox.app/v1/api")
# app.py refuses to import with no auth method configured.
os.environ.setdefault("INSECURE_ALLOW_ANON", "true")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# apscheduler and flask_limiter are not installed in the test env. The limiter
# decorators must stay pass-through or the routes they wrap become MagicMocks and
# Flask's registration fails at import.
for _m in ("apscheduler", "apscheduler.schedulers", "apscheduler.schedulers.background",
           "apscheduler.triggers", "apscheduler.triggers.cron",
           "apscheduler.triggers.interval"):
    sys.modules.setdefault(_m, MagicMock())
if "flask_limiter" not in sys.modules:
    _fl = MagicMock()
    _identity = lambda *a, **k: (lambda f: f)  # noqa: E731
    _fl.Limiter.return_value.limit = _identity
    _fl.Limiter.return_value.exempt = _identity
    sys.modules["flask_limiter"] = _fl
    sys.modules["flask_limiter.util"] = MagicMock()

import app  # noqa: E402


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code
        self.headers = {}


@pytest.fixture(autouse=True)
def _clear_alive_cache():
    app._spore_alive_cache.clear()
    yield
    app._spore_alive_cache.clear()


def _stub_head(monkeypatch, status=None, exc=None):
    """Point app's local `import requests as _req` at a stub, counting calls."""
    calls = []

    def _head(url, **kwargs):
        calls.append(url)
        if exc is not None:
            raise exc
        return _Resp(status)

    fake_requests = MagicMock()
    fake_requests.head = _head
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    return calls


@pytest.mark.parametrize("status", [200, 206, 302])
def test_ok_status_is_alive_and_cached(monkeypatch, status):
    calls = _stub_head(monkeypatch, status=status)
    assert app._cdn_url_alive("https://cdn.test/a.mkv") is True
    # Second call inside the TTL must not spend another CDN round trip.
    assert app._cdn_url_alive("https://cdn.test/a.mkv") is True
    assert len(calls) == 1


@pytest.mark.parametrize("status", sorted({400, 401, 403, 404, 410}))
def test_link_invalid_statuses_are_dead(monkeypatch, status):
    _stub_head(monkeypatch, status=status)
    assert app._cdn_url_alive("https://cdn.test/dead.mkv") is False
    assert "https://cdn.test/dead.mkv" not in app._spore_alive_cache


def test_429_is_alive_not_dead(monkeypatch):
    """The regression this carve-out exists for: a throttled link is still valid.
    Calling it dead re-resolves, TorBox returns the same URL, and the request rate
    feeding the 429 doubles."""
    _stub_head(monkeypatch, status=429)
    assert app._cdn_url_alive("https://cdn.test/busy.mkv") is True


def test_429_is_not_cached_as_alive(monkeypatch):
    """Alive-but-busy must not be memoised: nothing was confirmed, so the next
    request should re-check rather than trust a throttle for 120s."""
    _stub_head(monkeypatch, status=429)
    app._cdn_url_alive("https://cdn.test/busy.mkv")
    assert "https://cdn.test/busy.mkv" not in app._spore_alive_cache


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_transient_server_errors_keep_the_url(monkeypatch, status):
    _stub_head(monkeypatch, status=status)
    assert app._cdn_url_alive("https://cdn.test/x.mkv") is True


def test_head_failure_assumes_alive(monkeypatch):
    """A HEAD that never answered is not evidence the link expired."""
    _stub_head(monkeypatch, exc=OSError("connection reset"))
    assert app._cdn_url_alive("https://cdn.test/x.mkv") is True
