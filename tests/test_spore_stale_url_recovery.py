"""
Regression tests for stale-CDN-URL recovery in the /spore-stream proxy paths.

The bug: both the warm (virtual fast-start MP4) and cold (raw passthrough)
generators reacted to a failed CDN fetch by calling catbox.materialize() for a
"fresh" URL, but materialize() consults the same URL cache the stale URL came
from (23h TTL, while TorBox retires links sooner). It returned the identical
dead URL, `fresh != url_ref` was False, and the stream broke with 0 bytes.
Plex's transcoder re-opened the stream every few seconds, looping on
"CDN HTTP 400" until the cache entry expired: the "stuck buffering" incident
of 2026-07-24 (The Nanny S03E13).

The rule under test: a dead-URL fetch error (400/403/404...) must invalidate
the token's URL cache entry BEFORE re-materializing, so the retry uses a truly
fresh link. A 429 must NOT re-resolve at all: the URL is alive but throttled,
and TorBox hands back the same URL, so re-resolving only doubles the request
rate feeding the throttle (same line drawn by app._CDN_DEAD_STATUSES and
spore-nfs errRateLimited).
"""
import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("TORBOX_API_KEY", "test")
os.environ.setdefault("MEDIA_PATH", "/tmp/mycelium-test-media")
os.environ.setdefault("SPORE_MEDIA_PATH", "/tmp/mycelium-test-spore")
os.environ.setdefault("TORBOX_BASE_URL", "https://api.torbox.app/v1/api")
# app.py refuses to import with no auth method configured.
os.environ.setdefault("INSECURE_ALLOW_ANON", "true")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# apscheduler and flask_limiter are not installed in the test env. The limiter
# decorators must stay pass-through or the routes they wrap become MagicMocks
# and Flask's registration fails at import.
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

import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import catbox  # noqa: E402
import mp4_faststart  # noqa: E402

# conftest.py forces sys.modules["mp4_faststart"] to a MagicMock (the object the
# route also imports, so the monkeypatched serve_bytes/fetch_range below reach
# it). Raising needs the REAL exception class though: raising a MagicMock is a
# TypeError, which app.py catches as a status-less error and routes down the
# wrong (any-exception) branch, masking the 429 behavior under test.
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location(
    "_mp4_faststart_real",
    os.path.join(os.path.dirname(__file__), "..", "mp4_faststart.py"),
)
_real_fs = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_real_fs)
SporeFetchError = _real_fs.SporeFetchError

STALE = "https://cdn.test/stale"
FRESH = "https://cdn.test/fresh"
SIZE = 40


@pytest.fixture()
def client():
    return app_mod.app.test_client()


@pytest.fixture()
def harness(monkeypatch):
    """Stub the catbox/mp4_faststart seams around the /spore-stream route.

    `calls` records the order of materialize/invalidate calls; `urls["current"]`
    models the URL cache: invalidate flips it to FRESH, so a materialize that
    was NOT preceded by invalidate keeps returning STALE (the buggy behavior).
    """
    calls = []
    urls = {"current": STALE}

    def fake_materialize(token, allow_readd=None):
        calls.append("materialize")
        return urls["current"]

    def fake_invalidate(token=None):
        calls.append(("invalidate", token))
        urls["current"] = FRESH

    monkeypatch.setattr(catbox, "materialize", fake_materialize)
    monkeypatch.setattr(catbox, "invalidate_url_cache", fake_invalidate)
    monkeypatch.setattr(app_mod, "_enqueue_flip", lambda token: None)
    monkeypatch.setattr(
        app_mod.spore_readthrough,
        "read_range",
        lambda source, start, end, size, fetcher: fetcher(start, end),
    )
    yield calls, urls
    app_mod._spore_cold_sizes.clear()


def _warm_info():
    return {"already_fast": False, "ftyp_size": 64, "moov_size": 100,
            "cdn_size": SIZE, "header": b""}


def test_warm_path_dead_url_invalidates_then_retries_fresh(client, harness, monkeypatch):
    calls, _ = harness
    monkeypatch.setattr(mp4_faststart, "load", lambda token: _warm_info())

    def fake_serve(info, url, start, end, raw_fetch=None):
        if url == STALE:
            raise SporeFetchError("CDN HTTP 400", status=400)
        return b"F" * (end - start + 1)

    monkeypatch.setattr(mp4_faststart, "serve_bytes", fake_serve)

    resp = client.get("/spore-stream/warmtok400",
                      headers={"Range": f"bytes=0-{SIZE - 1}"})
    assert resp.status_code == 206
    assert resp.data == b"F" * SIZE
    # Route-top materialize, then invalidate BEFORE the recovery materialize.
    assert calls == ["materialize", ("invalidate", "warmtok400"), "materialize"]


def test_warm_path_429_does_not_reresolve(client, harness, monkeypatch):
    calls, _ = harness
    monkeypatch.setattr(mp4_faststart, "load", lambda token: _warm_info())

    def fake_serve(info, url, start, end, raw_fetch=None):
        raise SporeFetchError("CDN rate-limited (429)", status=429, retry_after=7)

    monkeypatch.setattr(mp4_faststart, "serve_bytes", fake_serve)

    resp = client.get("/spore-stream/warmtok429",
                      headers={"Range": f"bytes=0-{SIZE - 1}"})
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "7"
    assert b"temporarily busy" in resp.data
    # Only the route-top materialize: no invalidate, no re-resolve.
    assert calls == ["materialize"]


def test_cold_path_dead_url_invalidates_then_retries_fresh(client, harness, monkeypatch):
    calls, _ = harness
    monkeypatch.setattr(mp4_faststart, "load", lambda token: None)
    app_mod._spore_cold_sizes["coldtok400"] = SIZE

    def fake_fetch(url, start, end, **kwargs):
        if url == STALE:
            raise SporeFetchError("CDN HTTP 400", status=400)
        return b"F" * (end - start + 1)

    monkeypatch.setattr(mp4_faststart, "fetch_range", fake_fetch)

    resp = client.get("/spore-stream/coldtok400",
                      headers={"Range": f"bytes=0-{SIZE - 1}"})
    assert resp.status_code == 206
    assert resp.data == b"F" * SIZE
    assert calls == ["materialize", ("invalidate", "coldtok400"), "materialize"]


def test_cold_path_429_does_not_reresolve(client, harness, monkeypatch):
    calls, _ = harness
    monkeypatch.setattr(mp4_faststart, "load", lambda token: None)
    app_mod._spore_cold_sizes["coldtok429"] = SIZE

    def fake_fetch(url, start, end, **kwargs):
        raise SporeFetchError("CDN rate-limited (429)", status=429, retry_after=6)

    monkeypatch.setattr(mp4_faststart, "fetch_range", fake_fetch)

    resp = client.get("/spore-stream/coldtok429",
                      headers={"Range": f"bytes=0-{SIZE - 1}"})
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "6"
    assert calls == ["materialize"]


def test_non_mp4_uses_shared_proxy_instead_of_cdn_redirect(client, harness, monkeypatch):
    monkeypatch.setattr(
        mp4_faststart,
        "load",
        lambda token: {
            "already_fast": True,
            "ftyp_size": 0,
            "moov_size": 0,
            "cdn_size": SIZE,
            "header": b"",
        },
    )
    monkeypatch.setattr(
        mp4_faststart,
        "fetch_range",
        lambda url, start, end, **kwargs: b"M" * (end - start + 1),
    )
    resp = client.get("/spore-stream/mkvtok",
                      headers={"Range": f"bytes=0-{SIZE - 1}"})
    assert resp.status_code == 206
    assert resp.data == b"M" * SIZE
    assert "Location" not in resp.headers
