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
SIZE = 128 * 1024


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
    # These tests isolate byte-fetch recovery, not the header builder.
    monkeypatch.setattr(mp4_faststart, "build_and_cache", lambda *a, **k: False)
    monkeypatch.setattr(mp4_faststart, "MIN_MEDIA_BYTES", _real_fs.MIN_MEDIA_BYTES)

    def fake_materialize(token, allow_readd=None, **kwargs):
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
    _wait_for_builders()
    app_mod._spore_cold_sizes.clear()
    app_mod._spore_build_failed.clear()


def _wait_for_builders(timeout=5.0):
    import time
    deadline = time.monotonic() + timeout
    while app_mod._spore_probing and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not app_mod._spore_probing


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


# ── Header build: stale-link recovery and the cold-size poison ────────────────
# Everything below runs the real mp4_faststart builder and route against
# synthetic CDN responses only; no live token or provider is touched.

EBML = bytes.fromhex("1a45dfa3")  # MKV magic: the builder stores a non-MP4 sentinel


@pytest.fixture()
def real_builder(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "mp4_faststart", _real_fs)
    # The builder thread imports db itself; make it the module patched below
    # even when an earlier test module swapped in the real one.
    monkeypatch.setitem(sys.modules, "db", app_mod.db)
    _real_fs.init(tmp_path)
    monkeypatch.setattr(app_mod.enrichment, "local_stream_path", lambda token: None)
    monkeypatch.setattr(app_mod, "_enqueue_flip", lambda token: None)
    monkeypatch.setattr(app_mod, "_spore_source_key", lambda token: token)
    # Tracks already probed, so a finished build never shells out to ffprobe.
    monkeypatch.setattr(app_mod.db, "load_spore_tracks",
                        lambda token: {"preferred_audio_idx": 0})
    monkeypatch.setattr(app_mod.spore_readthrough, "read_range",
                        lambda key, start, end, size, fetch: fetch(start, end))
    media = EBML + b"M" * (SIZE - len(EBML))
    state = {"url": STALE, "status": 400, "recover": False, "calls": [],
             "heads": [], "builds": 0}

    def materialize(token, **kwargs):
        state["calls"].append(("materialize", kwargs))
        return state["url"]

    def invalidate(token=None):
        state["calls"].append(("invalidate", token))
        if state["recover"]:
            state["url"] = FRESH

    class Head:
        def __init__(self, url):
            self.status_code = state["status"] if url == STALE else 200
            # The retired link's error body: the length that used to be cached.
            self.headers = {"Content-Length": str(2250 if url == STALE else SIZE)}
            self.closed = False

        def close(self):
            self.closed = True

    def head(url, **kwargs):
        out = Head(url)
        state["heads"].append(out)
        return out

    def fetch(url, start, end, **kwargs):
        if url == STALE:
            raise SporeFetchError(f"CDN HTTP {state['status']}", status=state["status"])
        return media[start:end + 1]

    real_build = _real_fs.build_and_cache

    def counted_build(*args, **kwargs):
        state["builds"] += 1
        return real_build(*args, **kwargs)

    monkeypatch.setattr(catbox, "materialize", materialize)
    monkeypatch.setattr(catbox, "invalidate_url_cache", invalidate)
    monkeypatch.setattr(_real_fs.req_lib, "head", head)
    monkeypatch.setattr(_real_fs, "_get", fetch)
    monkeypatch.setattr(_real_fs, "fetch_range", fetch)
    monkeypatch.setattr(_real_fs, "build_and_cache", counted_build)
    app_mod._spore_cold_sizes.clear()
    app_mod._spore_build_failed.clear()
    yield state, media
    _wait_for_builders()
    assert all(h.closed for h in state["heads"])
    app_mod._spore_cold_sizes.clear()
    app_mod._spore_build_failed.clear()


def test_failed_cold_build_self_heals_without_restart(client, real_builder):
    """The brief's repro: a stale link fails the build, then playback recovers."""
    state, media = real_builder
    tok = "synthetic-heal"
    # 1-3: no fresh link exists yet. The build fails and no size is invented.
    first = client.get(f"/spore-stream/{tok}", headers={"Range": "bytes=0-4095"})
    first.close()
    assert first.status_code == 502
    assert _real_fs.load(tok) is None
    assert tok not in app_mod._spore_cold_sizes
    # The in-process state that used to stick until a restart.
    app_mod._spore_cold_sizes[tok] = 2250
    # 4-5: the link can be re-resolved again. The next request serves the real
    # size straight away, inside the build's failure backoff.
    state["recover"] = True
    second = client.get(f"/spore-stream/{tok}", headers={"Range": "bytes=0-4095"})
    assert second.status_code == 206
    assert second.headers["Content-Range"] == f"bytes 0-4095/{SIZE}"
    assert second.data == media[:4096]
    second.close()
    assert app_mod._spore_cold_sizes[tok] == SIZE
    # Once the backoff lapses the header builds too, still without a restart.
    _wait_for_builders()
    app_mod._spore_build_failed.clear()
    third = client.get(f"/spore-stream/{tok}", headers={"Range": "bytes=0-15"})
    assert third.status_code == 206
    assert third.headers["Content-Range"] == f"bytes 0-15/{SIZE}"
    third.close()
    assert _real_fs.load(tok)["cdn_size"] == SIZE


def test_failed_build_is_not_retried_on_every_range_request(client, real_builder):
    state, _ = real_builder
    for _ in range(3):
        resp = client.get("/spore-stream/synthetic-backoff",
                          headers={"Range": "bytes=0-15"})
        resp.close()
        assert resp.status_code == 502
        _wait_for_builders()
    assert state["builds"] == 1
    app_mod._spore_build_failed.clear()
    client.get("/spore-stream/synthetic-backoff", headers={"Range": "bytes=0-15"}).close()
    _wait_for_builders()
    assert state["builds"] == 2


def test_build_recovers_stale_head_once(real_builder):
    state, _ = real_builder
    state["recover"] = True
    assert _real_fs.build_and_cache(STALE, "synthetic-head")
    assert state["calls"] == [("invalidate", "synthetic-head"),
                              ("materialize", {"record_playback": False})]
    assert _real_fs.load("synthetic-head")["cdn_size"] == SIZE


def test_build_recovers_stale_range_once(real_builder, monkeypatch):
    state, _ = real_builder
    state["recover"] = True
    # HEAD can still answer while the first range read hits the retired link.
    ok_head = type("Head", (), {"status_code": 200,
                                "headers": {"Content-Length": str(SIZE)},
                                "close": lambda self: None})
    monkeypatch.setattr(_real_fs.req_lib, "head", lambda *a, **k: ok_head())
    assert _real_fs.build_and_cache(STALE, "synthetic-range")
    assert [c[0] for c in state["calls"]] == ["invalidate", "materialize"]


@pytest.mark.parametrize("status", [401, 403, 404, 410, 429, 503])
def test_build_auth_gone_busy_and_server_errors_do_not_refresh(real_builder, status):
    state, _ = real_builder
    state.update(status=status, recover=True)
    assert not _real_fs.build_and_cache(STALE, "synthetic-terminal")
    assert state["calls"] == []
    assert _real_fs.load("synthetic-terminal") is None


def test_build_refresh_is_bounded_to_one_retry(real_builder, monkeypatch):
    state, _ = real_builder
    state["recover"] = True

    def always_stale(*args, **kwargs):
        raise SporeFetchError("CDN HTTP 400", status=400)

    monkeypatch.setattr(_real_fs, "_get", always_stale)
    assert not _real_fs.build_and_cache(STALE, "synthetic-twice")
    assert [c[0] for c in state["calls"]] == ["invalidate", "materialize"]


@pytest.mark.parametrize("size", [0, 283, 2250, 64 * 1024 - 1])
def test_successful_head_below_media_floor_is_not_a_size(real_builder, monkeypatch, size):
    state, _ = real_builder
    tiny = type("Head", (), {"status_code": 200,
                             "headers": {"Content-Length": str(size)},
                             "close": lambda self: None})
    monkeypatch.setattr(_real_fs.req_lib, "head", lambda *a, **k: tiny())
    with pytest.raises(SporeFetchError, match="below media floor"):
        _real_fs.probe_size(FRESH)
    assert not _real_fs.build_and_cache(FRESH, "synthetic-small")
    assert _real_fs.load("synthetic-small") is None
    assert state["calls"] == []


def test_warm_request_waits_for_header_not_probe(client, real_builder, monkeypatch):
    import threading
    import time
    state, media = real_builder
    state["url"] = FRESH
    probing, release = threading.Event(), threading.Event()

    def slow_probe_stage(token):
        # Only the background builder blocks; the route's own lookup answers.
        if threading.current_thread().name.startswith("fsh-"):
            probing.set()
            assert release.wait(10)
        return {"preferred_audio_idx": 0}

    monkeypatch.setattr(app_mod.db, "load_spore_tracks", slow_probe_stage)
    try:
        started = time.monotonic()
        resp = client.get("/spore-stream/synthetic-warm", headers={"Range": "bytes=0-15"})
        assert resp.status_code == 206
        assert resp.data == media[:16]
        resp.close()
        assert probing.wait(2)
        assert time.monotonic() - started < 4.0
    finally:
        release.set()


def test_concurrent_requests_share_one_inflight_build(client, real_builder, monkeypatch):
    import threading
    state, media = real_builder
    state["url"] = FRESH
    entered, release = threading.Event(), threading.Event()
    original = _real_fs.build_and_cache

    def blocked(*args, **kwargs):
        state["builds"] += 1
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(_real_fs, "build_and_cache", blocked)
    result = []

    def request_first():
        with app_mod.app.test_client() as other:
            resp = other.get("/spore-stream/synthetic-shared", headers={"Range": "bytes=0-15"})
            result.append(resp.status_code)
            resp.close()

    first = threading.Thread(target=request_first)
    first.start()
    try:
        assert entered.wait(2)
        # A second request neither waits on nor launches another builder.
        resp = client.get("/spore-stream/synthetic-shared", headers={"Range": "bytes=0-15"})
        assert resp.status_code == 206
        assert resp.data == media[:16]
        resp.close()
        assert state["builds"] == 1
    finally:
        release.set()
        first.join(10)
    assert not first.is_alive()
    assert result == [206]


class _Resp:
    def __init__(self, status):
        self.status_code = status
        self.headers = {}

    def close(self):
        pass


@pytest.mark.parametrize("status,expected_tries", [(400, 1), (403, 1), (408, 4), (500, 4)])
def test_fetch_client_errors_fail_fast_and_transient_ones_retry(monkeypatch, status,
                                                                 expected_tries):
    sent = []
    monkeypatch.setattr(_real_fs.req_lib, "get",
                        lambda *a, **k: sent.append(1) or _Resp(status))
    monkeypatch.setattr(_real_fs.time, "sleep", lambda s: None)
    with pytest.raises(SporeFetchError) as exc:
        _real_fs._get(FRESH, 0, 15, tries=4)
    assert exc.value.status == status
    assert len(sent) == expected_tries


def test_flip_rescan_queues_a_guarded_scan_and_never_refreshes_plex(monkeypatch):
    import media_servers
    marked, called = [], []
    monkeypatch.setattr(media_servers, "mark", marked.append)
    monkeypatch.setattr(app_mod._os, "environ", {
        "PLEX_URL": "http://plex.test:32400", "PLEX_TOKEN": "synthetic",
        "PLEX_SERIES_SECTION": "8", "PLEX_MOVIE_SECTION": "7",
    })
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: called.append(a))
    strm = "/data/media/series/Example/Season 01/Example S01E01.strm"
    app_mod._spore_plex_rescan({"media_type": "series", "strm_path": strm})
    app_mod._spore_plex_rescan({"media_type": "series", "strm_path": None})
    assert marked == [strm]
    assert called == []

