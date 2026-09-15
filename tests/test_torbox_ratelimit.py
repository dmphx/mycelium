"""A 429 from TorBox must open a cooldown and fall back to the cached list.

Regression test for 2026-09-15: list_torrents only cached on success, so a rate
limit left the cache empty and every later call hit the API again, keeping the
account rate limited until playback itself 404'd.
"""
import importlib
import sys

import pytest
import requests


@pytest.fixture
def torbox(monkeypatch):
    """Load the real torbox module.

    conftest force-mocks sys.modules["torbox"] for the whole session; monkeypatch
    restores the mock at teardown so later modules keep the harness they expect.
    """
    monkeypatch.delitem(sys.modules, "torbox", raising=False)
    real = importlib.import_module("torbox")
    monkeypatch.setitem(sys.modules, "torbox", real)
    return real


class _Resp:
    def __init__(self, status_code=429, headers=None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("boom", response=self)


def test_429_without_cache_raises_rate_limited(torbox, monkeypatch):
    monkeypatch.setattr(torbox.requests, "get", lambda *a, **k: _Resp(429))
    with pytest.raises(torbox.TorBoxRateLimited):
        torbox.list_torrents(force_refresh=True)
    assert torbox._rate_limited_now()


def test_rate_limited_is_a_request_exception(torbox):
    assert issubclass(torbox.TorBoxRateLimited, requests.exceptions.RequestException)


def test_429_serves_stale_cache_and_stops_calling(torbox, monkeypatch):
    calls = []

    def fake_get(*a, **k):
        calls.append(k.get("params"))
        return _Resp(429)

    torbox._mylist_cache["items"] = [{"id": 1, "hash": "abc"}]
    torbox._mylist_cache["ts"] = -10_000.0  # long expired
    monkeypatch.setattr(torbox.requests, "get", fake_get)

    assert torbox.list_torrents(force_refresh=True) == [{"id": 1, "hash": "abc"}]
    hits_after_first = len(calls)
    for _ in range(5):
        assert torbox.list_torrents(force_refresh=True) == [{"id": 1, "hash": "abc"}]
    assert len(calls) == hits_after_first, "cooldown must stop further API calls"


def test_retry_after_header_sets_cooldown(torbox, monkeypatch):
    monkeypatch.setattr(torbox.requests, "get",
                        lambda *a, **k: _Resp(429, headers={"Retry-After": "120"}))
    with pytest.raises(torbox.TorBoxRateLimited):
        torbox.list_torrents(force_refresh=True)
    import time
    remaining = torbox._mylist_cooldown_until - time.monotonic()
    assert 100 < remaining <= torbox._MYLIST_COOLDOWN_MAX


def test_find_by_id_skips_during_cooldown(torbox, monkeypatch):
    calls = []
    monkeypatch.setattr(torbox.requests, "get",
                        lambda *a, **k: calls.append(1) or _Resp(429))
    torbox._note_rate_limit(None)
    assert torbox.find_by_id(123) is None
    assert calls == [], "find_by_id must not call TorBox while cooling down"
