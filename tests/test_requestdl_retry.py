"""
Regressietests voor de TorBox requestdl retry + single-flight.

Borgt twee dingen rond playback-resolutie:
  1. _requestdl_get retried transient 5xx EN 429 (rate-limit), maar nooit een
     deterministische 4xx (404/403). Achtergrond: een 429 op de eerste play van
     een bestand viel direct door naar een mycelium 404 ("fatal player error").
  2. _requestdl_single_flight bundelt gelijktijdige identieke calls: bij een
     burst (Jellyfin vuurt meerdere ffmpeg-probes tegelijk af) gaat er maar EEN
     naar TorBox; de rest hergebruikt het resultaat. Dat was de burst die de
     429 veroorzaakte.

Geen live DB of netwerk nodig; zware imports worden gemockt.
"""
import os
import sys
import threading
import time as _time
from unittest.mock import MagicMock

os.environ.setdefault("TORBOX_API_KEY", "test")
os.environ.setdefault("MEDIA_PATH", "/tmp/mycelium-test-media")
os.environ.setdefault("SPORE_MEDIA_PATH", "/tmp/mycelium-test-spore")
os.environ.setdefault("TORBOX_BASE_URL", "https://api.torbox.app/v1/api")

for _mod in ("db", "jellyfin", "settings", "torbox", "nfo_generator", "mp4_faststart"):
    sys.modules.setdefault(_mod, MagicMock())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import strm_generator as sg  # noqa: E402  (imports na sys.path setup)


class _Resp:
    def __init__(self, status, data=None, headers=None):
        self.status_code = status
        self._data = data
        self.headers = headers or {}

    def json(self):
        return {"data": self._data} if self._data is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise sg.req_lib.HTTPError(f"HTTP {self.status_code}")


def _patch_get(monkeypatch, responses):
    """Feed a fixed sequence of responses to req_lib.get and count calls."""
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params))
        return responses.pop(0)

    monkeypatch.setattr(sg.req_lib, "get", fake_get)
    monkeypatch.setattr(sg.time, "sleep", lambda *_a, **_k: None)  # no real backoff
    return calls


def test_429_then_success_is_retried(monkeypatch):
    calls = _patch_get(monkeypatch, [
        _Resp(429, headers={"Retry-After": "0"}),
        _Resp(200, data="cdn://ok"),
    ])
    assert sg._requestdl_get("u", {}, "lbl") == "cdn://ok"
    assert len(calls) == 2


def test_5xx_then_success_is_retried(monkeypatch):
    calls = _patch_get(monkeypatch, [_Resp(500), _Resp(200, data="cdn://ok")])
    assert sg._requestdl_get("u", {}, "lbl") == "cdn://ok"
    assert len(calls) == 2


def test_404_is_not_retried(monkeypatch):
    calls = _patch_get(monkeypatch, [_Resp(404)])
    assert sg._requestdl_get("u", {}, "lbl") is None
    assert len(calls) == 1


def test_403_is_not_retried(monkeypatch):
    calls = _patch_get(monkeypatch, [_Resp(403)])
    assert sg._requestdl_get("u", {}, "lbl") is None
    assert len(calls) == 1


def test_retry_after_is_capped(monkeypatch):
    # A huge Retry-After must not stall playback: capped to the config value.
    captured = []
    monkeypatch.setattr(sg.req_lib, "get", lambda *a, **k: _Resp(
        429, headers={"Retry-After": "9999"}) if not captured else _Resp(200, data="ok"))

    def fake_sleep(d):
        captured.append(d)

    monkeypatch.setattr(sg.time, "sleep", fake_sleep)
    assert sg._requestdl_get("u", {}, "lbl") == "ok"
    assert captured and captured[0] <= float(sg.cfg.REQUESTDL_RETRY_AFTER_CAP_SEC)


def test_single_flight_coalesces_concurrent_calls():
    calls = []
    in_fn = threading.Event()
    release = threading.Event()

    def slow_fn():
        calls.append(1)
        in_fn.set()
        release.wait(5)
        return "cdn://ok"

    results = [None] * 5

    def worker(i):
        results[i] = sg._requestdl_single_flight("sf:test:1", slow_fn)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    threads[0].start()
    assert in_fn.wait(2)          # leader is inside fn -> key is registered
    for t in threads[1:]:
        t.start()
    _time.sleep(0.2)              # let followers reach event.wait()
    release.set()
    for t in threads:
        t.join(5)

    assert calls == [1]                       # fn ran exactly once
    assert results == ["cdn://ok"] * 5        # all callers got the URL
    assert sg._requestdl_sf == {}             # registry cleaned up
