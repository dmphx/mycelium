"""Background createtorrent callers must leave headroom for playback.

Regression test for 2026-09-16: season-pack consolidation held the shared
budget at 58/60 all day, so a cold on-play re-add was refused and Jellyfin
got a 404 ("fatal player error").
"""
import importlib
import sys
import time

import pytest


@pytest.fixture
def torbox(monkeypatch):
    monkeypatch.delitem(sys.modules, "torbox", raising=False)
    real = importlib.import_module("torbox")
    monkeypatch.setitem(sys.modules, "torbox", real)
    monkeypatch.setattr(real, "_CREATETORRENT_LOADED", True)
    monkeypatch.setattr(real, "_CREATETORRENT_LOG", real.deque(maxlen=200))
    return real


def _fill(torbox, n, reason="upgrade-pack"):
    # Spread over the hour so the per-minute burst limit doesn't trip.
    now = time.time()
    for i in range(n):
        torbox._CREATETORRENT_LOG.append((now - 3000 + i, reason))


def test_background_stops_at_background_cap(torbox):
    _fill(torbox, torbox._BACKGROUND_LIMIT_HOUR)
    with pytest.raises(torbox.RateLimited, match="hourly quota"):
        torbox._reserve_createtorrent_slot("upgrade-pack")


def test_playback_still_has_headroom_after_background_cap(torbox):
    _fill(torbox, torbox._BACKGROUND_LIMIT_HOUR)
    entry = torbox._reserve_createtorrent_slot("catbox-readd")
    assert entry[1] == "catbox-readd"


def test_playback_stops_below_the_real_limit(torbox):
    _fill(torbox, torbox._CREATETORRENT_LIMIT_HOUR - 2, reason="catbox-readd")
    with pytest.raises(torbox.RateLimited):
        torbox._reserve_createtorrent_slot("catbox-readd")


def test_background_per_minute_burst_is_lower(torbox):
    now = time.time()
    for i in range(torbox._BACKGROUND_LIMIT_MIN):
        torbox._CREATETORRENT_LOG.append((now - i, "upgrade"))
    with pytest.raises(torbox.RateLimited, match="per-minute"):
        torbox._reserve_createtorrent_slot("upgrade")
    torbox._reserve_createtorrent_slot("web_player")
